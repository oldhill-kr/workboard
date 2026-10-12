"""업무보드: Obsidian Work/업무 노트 + 구글 캘린더(iCal)를 달력·카드로 보여주는 로컬 웹 서버.

실행: python server.py  →  http://127.0.0.1:8765
데이터는 Obsidian 노트가 원본이다. 이 서버는 노트를 읽고, 상태·마감·체크리스트·진행 기록만 고쳐 쓴다.
"""
import json
import os
import re
import threading
import time
import urllib.request
import webbrowser
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

import icalendar
import recurring_ical_events

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"
DEFAULT_CONFIG = {
    "work_dir": str(Path.home() / "OneDrive" / "문서" / "Obsidian Vault" / "Work"),
    "vault_name": "Obsidian Vault",
    "ics_urls": [],
    "port": 8765,
    # 알림 시각 (HH:MM). 비워 두면 그 알림은 끔
    "alarms": {"morning": "08:40", "evening": "17:00", "friday": "16:00"},
}
STATE_PATH = ROOT / "alarm_state.json"
STATUSES = ["할일", "진행중", "대기", "완료", "보류"]
# 프로젝트 색 팔레트 (index.html의 PALETTE와 같은 순서·값이어야 한다)
PALETTE = ["#3b6ff5", "#e5484d", "#2f9e5b", "#d98a00", "#8e4ec6", "#0f9fb5", "#d6409f", "#6b7a2e", "#c4622d", "#5b6b8c", "#84a800", "#a21caf", "#38a3f5", "#f97362", "#10b981", "#6d28d9", "#8b5e3c", "#be123c", "#c2a000", "#6c8ebf"]
CHECK_RE = re.compile(r"^(\s*[-*] \[)( |x|X)(\] )(.*)$")
FM_RE = re.compile(r"^---\r?\n(.*?)\r?\n---\r?\n?", re.S)
lock = threading.Lock()


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG_PATH.exists():
        cfg.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
    return cfg


def save_config(cfg):
    CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")


def tasks_dir():
    return Path(load_config()["work_dir"]) / "업무"


# ---------- 노트 읽기/쓰기 ----------

def parse_value(raw):
    raw = raw.strip()
    if raw.startswith("[") and raw.endswith("]"):
        return [v.strip().strip("'\"") for v in raw[1:-1].split(",") if v.strip()]
    return raw.strip("'\"")


def read_note(path):
    text = path.read_text(encoding="utf-8")
    m = FM_RE.match(text)
    fm, body = {}, text
    if m:
        for line in m.group(1).splitlines():
            if ":" in line and not line.startswith((" ", "\t")):
                k, v = line.split(":", 1)
                fm[k.strip()] = parse_value(v)
        body = text[m.end():]
    return fm, body, text


def task_from_file(path):
    fm, body, _ = read_note(path)
    if fm.get("type") != "업무":
        return None
    checks = []
    for line in body.splitlines():
        cm = CHECK_RE.match(line)
        if cm:
            checks.append({"text": cm.group(4), "done": cm.group(2) != " "})
    title = path.stem
    for line in body.splitlines():
        if line.startswith("# "):
            title = line[2:].strip()
            break
    rel = f"Work/업무/{path.stem}"
    vault = load_config()["vault_name"]
    people = fm.get("people") or []
    return {
        "id": path.name,
        "title": title,
        "project": fm.get("project") or "기타",
        "status": fm.get("status") or "할일",
        "priority": fm.get("priority") or "보통",
        "due": fm.get("due") or None,
        "created": fm.get("created") or None,
        "completed": fm.get("completed") or None,
        "archived": str(fm.get("archived", "")).lower() == "true",
        "people": people if isinstance(people, list) else [people],
        "checks": checks,
        "body": body,
        "mtime": path.stat().st_mtime,
        "obsidian": f"obsidian://open?vault={quote(vault)}&file={quote(rel)}",
    }


def archived_projects():
    """노트가 전부 보관(archived: true)된 프로젝트 이름. 보관 상태의 원본은 노트 머리말이다."""
    state = {}
    for t in list_tasks(include_archived=True):
        state[t["project"]] = state.get(t["project"], True) and t["archived"]
    return {p for p, v in state.items() if v}


def set_archived(project, flag):
    """프로젝트의 모든 업무 노트에 archived: true 를 넣거나(보관) 뺀다(해제). 수정 시각은 그대로 둔다."""
    project = (project or "").strip()
    if not project:
        raise ValueError("프로젝트 이름이 필요해요")
    d = tasks_dir()
    with lock:
        n = 0
        for p in sorted(d.glob("*.md")):
            fm, _, text = read_note(p)
            if fm.get("type") != "업무" or (fm.get("project") or "기타") != project:
                continue
            st = p.stat()
            new = set_frontmatter(text, "archived", "true") if flag else remove_frontmatter(text, "archived")
            if new != text:
                p.write_text(new, encoding="utf-8")
                os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns))
            n += 1
        if not n:
            raise ValueError("그 프로젝트의 업무 노트를 찾지 못했어요")


def archived_summary():
    """보관된 프로젝트별 (이름, 업무 수, 완료 수, 마지막 활동일)."""
    names = archived_projects()
    out = {}
    for t in list_tasks(include_archived=True):
        if t["project"] in names:
            o = out.setdefault(t["project"], {"project": t["project"], "total": 0, "done": 0, "mtime": 0})
            o["total"] += 1
            o["done"] += t["status"] == "완료"
            o["mtime"] = max(o["mtime"], t["mtime"])
    res = sorted(out.values(), key=lambda o: -o["mtime"])
    for o in res:
        o["last"] = datetime.fromtimestamp(o["mtime"]).date().isoformat() if o["mtime"] else None
    return res


def list_tasks(include_archived=False):
    d = tasks_dir()
    if not d.exists():
        return []
    out = []
    for p in sorted(d.glob("*.md")):
        try:
            t = task_from_file(p)
            if t and (include_archived or not t["archived"]):
                out.append(t)
        except Exception as e:  # 노트 하나가 깨져도 전체는 보여준다
            print("노트 읽기 실패:", p.name, e)
    return out


def safe_task_path(task_id):
    p = (tasks_dir() / task_id).resolve()
    if p.parent != tasks_dir().resolve() or p.suffix != ".md" or not p.exists():
        raise ValueError("잘못된 업무 id")
    return p


def set_frontmatter(text, key, value):
    m = FM_RE.match(text)
    if not m:
        return f"---\n{key}: {value}\n---\n" + text
    lines = m.group(1).splitlines()
    pat = re.compile(rf"^{re.escape(key)}\s*:")
    for i, line in enumerate(lines):
        if pat.match(line):
            lines[i] = f"{key}: {value}".rstrip()
            break
    else:
        lines.append(f"{key}: {value}".rstrip())
    nl = "\r\n" if "\r\n" in text[: m.end()] else "\n"
    return "---" + nl + nl.join(lines) + nl + "---" + nl + text[m.end():]


NOTE_BULLET_RE = re.compile(r"^(\s*[-*] )(?!\[[ xX]\] )(.*)$")


def append_log(text, entry, section="진행 기록", dated=True):
    """'## <섹션>' 끝에 글머리 한 줄 추가 (없으면 섹션을 만든다). 진행 기록은 날짜를 앞에 붙인다."""
    nl = "\r\n" if "\r\n" in text else "\n"
    line = f"- {date.today().isoformat()}: {entry}" if dated else f"- {entry}"
    lines = text.split(nl)
    try:
        start = next(i for i, l in enumerate(lines) if l.strip() == f"## {section}")
    except StopIteration:
        return text.rstrip() + nl + nl + f"## {section}" + nl + line + nl
    end = len(lines)
    for i in range(start + 1, len(lines)):
        if lines[i].startswith("## "):
            end = i
            break
    insert_at = end
    while insert_at - 1 > start and not lines[insert_at - 1].strip():
        insert_at -= 1
    lines.insert(insert_at, line)
    return nl.join(lines)


def remove_frontmatter(text, key):
    m = FM_RE.match(text)
    if not m:
        return text
    lines = [l for l in m.group(1).splitlines() if not re.match(rf"^{re.escape(key)}\s*:", l)]
    nl = "\r\n" if "\r\n" in text[: m.end()] else "\n"
    return "---" + nl + nl.join(lines) + nl + "---" + nl + text[m.end():]


def edit_note_line(text, section, idx, new_text):
    """'## <섹션>' 안의 idx번째 글머리 줄을 고친다. new_text가 None이면 줄을 지운다."""
    nl = "\r\n" if "\r\n" in text else "\n"
    lines = text.split(nl)
    start = next((i for i, l in enumerate(lines) if l.strip() == f"## {section}"), None)
    if start is None:
        raise ValueError("그 항목을 찾지 못했어요. 새로고침 후 다시 해 주세요")
    n = -1
    for i in range(start + 1, len(lines)):
        if lines[i].startswith("## "):
            break
        m = NOTE_BULLET_RE.match(lines[i])
        if not m:
            continue
        n += 1
        if n == idx:
            if new_text is None:
                del lines[i]
            else:
                lines[i] = m.group(1) + new_text
            return nl.join(lines)
    raise ValueError("그 항목을 찾지 못했어요. 새로고침 후 다시 해 주세요")


def edit_check_line(text, idx, new_text):
    """idx번째 체크박스 줄을 고친다. new_text가 None이면 줄을 지운다."""
    nl = "\r\n" if "\r\n" in text else "\n"
    n, out, found = -1, [], False
    for line in text.split(nl):
        cm = CHECK_RE.match(line)
        if cm:
            n += 1
            if n == idx:
                found = True
                if new_text is None:
                    continue
                line = cm.group(1) + cm.group(2) + cm.group(3) + new_text
        out.append(line)
    if not found:
        raise ValueError("할 일 항목을 찾지 못했어요. 새로고침 후 다시 해 주세요")
    return nl.join(out)


def add_check_line(text, item):
    """'## 할 일' 섹션의 마지막 체크박스 뒤에 추가 (빈 체크박스가 있으면 그 자리에)."""
    nl = "\r\n" if "\r\n" in text else "\n"
    lines = text.split(nl)
    new = f"- [ ] {item}"
    for i, l in enumerate(lines):
        cm = CHECK_RE.match(l)
        if cm and not cm.group(4).strip():
            lines[i] = new
            return nl.join(lines)
    try:
        start = next(i for i, l in enumerate(lines) if l.strip() == "## 할 일")
    except StopIteration:
        try:
            at = next(i for i, l in enumerate(lines) if l.strip() == "## 진행 기록")
        except StopIteration:
            at = len(lines)
        lines[at:at] = ["## 할 일", new, ""]
        return nl.join(lines)
    end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("## ")), len(lines))
    last = max((i for i in range(start + 1, end) if CHECK_RE.match(lines[i])), default=start)
    lines.insert(last + 1, new)
    return nl.join(lines)


def update_task(data):
    if data.get("project") is not None:  # 프로젝트 변경은 파일 이름이 바뀌므로 먼저 처리하고 새 id로 이어간다
        data = dict(data)
        data["id"] = change_task_project(data["id"], data.pop("project"))
    p = safe_task_path(data["id"])
    with lock:
        text = p.read_text(encoding="utf-8")
        if "status" in data:
            if data["status"] not in STATUSES:
                raise ValueError("잘못된 상태")
            old = read_note(p)[0].get("status")
            text = set_frontmatter(text, "status", data["status"])
            if data["status"] == "완료":
                text = set_frontmatter(text, "completed", date.today().isoformat())
            else:
                text = remove_frontmatter(text, "completed")
            if old != data["status"]:
                text = append_log(text, f"상태 {old} → {data['status']}")
        if "due" in data:
            due = data["due"] or ""
            if due and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", due):
                raise ValueError("마감일 형식은 YYYY-MM-DD")
            text = set_frontmatter(text, "due", due)
        if "priority" in data:
            text = set_frontmatter(text, "priority", data["priority"])
        if "toggle_check" in data:
            idx, n, out = int(data["toggle_check"]), -1, []
            nl = "\r\n" if "\r\n" in text else "\n"
            for line in text.split(nl):
                cm = CHECK_RE.match(line)
                if cm:
                    n += 1
                    if n == idx:
                        mark = " " if cm.group(2) != " " else "x"
                        line = cm.group(1) + mark + cm.group(3) + cm.group(4)
                out.append(line)
            text = nl.join(out)
        if "delete_check" in data:
            text = edit_check_line(text, int(data["delete_check"]), None)
        if "edit_check" in data:
            new = (data.get("text") or "").strip()
            if not new:
                raise ValueError("내용을 입력해 주세요")
            text = edit_check_line(text, int(data["edit_check"]), new)
        if data.get("add_check"):
            text = add_check_line(text, data["add_check"].strip())
        if data.get("log"):
            text = append_log(text, data["log"].strip())
        if data.get("add_note"):
            n = data["add_note"]
            sec, txt = (n.get("section") or "진행 기록").strip(), (n.get("text") or "").strip()
            if not txt:
                raise ValueError("내용을 입력해 주세요")
            text = append_log(text, txt, sec, dated=(sec == "진행 기록"))
        if data.get("edit_note"):
            n = data["edit_note"]
            txt = (n.get("text") or "").strip()
            if not txt:
                raise ValueError("내용을 입력해 주세요")
            text = edit_note_line(text, (n.get("section") or "").strip(), int(n["idx"]), txt)
        if data.get("delete_note"):
            n = data["delete_note"]
            text = edit_note_line(text, (n.get("section") or "").strip(), int(n["idx"]), None)
        p.write_text(text, encoding="utf-8", newline="")
    return task_from_file(p)


def delete_task(data):
    """노트를 지우지 않고 Work/_휴지통 폴더로 옮긴다 (옵시디언에서 되살릴 수 있음)."""
    with lock:
        p = safe_task_path(data["id"])
        trash = Path(load_config()["work_dir"]) / "_휴지통"
        trash.mkdir(exist_ok=True)
        dest = trash / p.name
        if dest.exists():
            dest = trash / f"{p.stem} ({datetime.now():%Y%m%d-%H%M%S}){p.suffix}"
        p.rename(dest)
    return {"id": data["id"], "trash": str(dest)}


BAD_NAME_RE = re.compile(r'[\\/:*?"<>|#^\[\]]')


def _check_project_name(new):
    if not new:
        raise ValueError("프로젝트 이름이 필요해요")
    if BAD_NAME_RE.search(new):
        raise ValueError('이름에 \\ / : * ? " < > | # ^ [ ] 는 쓸 수 없어요')


def _retarget_note(p, text, old, new, d):
    """노트 하나를 old → new 프로젝트로 옮긴다 (project 값, 파일 이름, H1 제목). (새 stem, 옛 stem) 반환."""
    stem, prefix = p.stem, f"{old} - "
    new_stem = f"{new} - {stem[len(prefix):]}" if stem.startswith(prefix) else stem
    text = set_frontmatter(text, "project", new)
    if new_stem != stem:  # 제목(H1)도 같은 접두어로
        text = re.sub(rf"^# {re.escape(stem)}\s*$", f"# {new_stem}", text, count=1, flags=re.M)
    dest, i = d / f"{new_stem}.md", 2
    while dest.exists() and dest != p:
        dest, i = d / f"{new_stem} ({i}).md", i + 1
    p.write_text(text, encoding="utf-8", newline="")
    if dest != p:
        p.rename(dest)
    return dest, stem


def _fix_links(moved):
    """Work 폴더 안 노트의 [[옛 이름]] 링크를 새 이름으로 (바이트 그대로 읽고 써서 줄바꿈을 건드리지 않는다)."""
    work, n = Path(load_config()["work_dir"]), 0
    for q in work.rglob("*.md"):
        if "_휴지통" in q.parts:
            continue
        try:
            t = q.read_bytes().decode("utf-8")
        except Exception:
            continue
        t2 = t
        for o, nn in moved.items():
            t2 = re.sub(r"\[\[" + re.escape(o) + r"(?=[\]|#])", "[[" + nn, t2)
        if t2 != t:
            q.write_bytes(t2.encode("utf-8"))
            n += 1
    return n


def _carry_color(old, new):
    """이름만 바꾼 경우(합치기가 아닐 때) 프로젝트 색을 그대로 가져간다."""
    with lock:
        colors = _load_colors() or {}
        if old in colors and new not in colors and not any(
                (t["project"] == old) for t in list_tasks(include_archived=True)):
            colors[new] = colors.pop(old)
            _save_colors(colors)


def rename_project(old, new):
    """프로젝트 이름을 바꾼다. 이미 있는 이름이면 그 프로젝트로 합쳐진다.
    노트의 project 필드, 파일 이름·제목의 '프로젝트 - ' 접두어, Work 폴더 안의 [[링크]]를 함께 고친다."""
    old, new = (old or "").strip(), (new or "").strip()
    _check_project_name(new)
    if not old:
        raise ValueError("프로젝트 이름이 필요해요")
    if old == new:
        raise ValueError("이름이 같아요")
    d = tasks_dir()
    moved = {}
    with lock:
        notes = []
        for p in sorted(d.glob("*.md")):
            fm, _, text = read_note(p)
            if fm.get("type") == "업무" and (fm.get("project") or "기타") == old:
                notes.append((p, text))
        if not notes:
            raise ValueError("그 프로젝트의 업무 노트를 찾지 못했어요")
        for p, text in notes:
            dest, stem = _retarget_note(p, text, old, new, d)
            if dest.stem != stem:
                moved[stem] = dest.stem
        links = _fix_links(moved) if moved else 0
    _carry_color(old, new)
    return {"count": len(notes), "renamed": len(moved), "links_updated": links}


def change_task_project(task_id, new):
    """업무 하나의 프로젝트만 바꾼다 (파일 이름·제목·링크 포함). 새 노트 id(파일명)를 돌려준다."""
    new = (new or "").strip()
    _check_project_name(new)
    d = tasks_dir()
    with lock:
        p = safe_task_path(task_id)
        fm, _, text = read_note(p)
        old = (fm.get("project") or "기타")
        if old == new:
            return p.name
        dest, stem = _retarget_note(p, text, old, new, d)
        if dest.stem != stem:
            _fix_links({stem: dest.stem})
    _carry_color(old, new)
    return dest.name


COLORS_FILE = ".업무보드-색상.json"  # 볼트(Work 폴더) 안에 둬서 PC가 달라도 같은 색을 쓴다. 점(.)으로 시작해 옵시디언에는 안 보인다


def _colors_path():
    return Path(load_config()["work_dir"]) / COLORS_FILE


def _load_colors():
    try:
        return json.loads(_colors_path().read_text(encoding="utf-8"))
    except Exception:
        return None  # 파일 없음 (처음 쓰거나 이전 버전에서 올라온 경우)


def _save_colors(colors):
    _colors_path().write_text(json.dumps(colors, ensure_ascii=False, indent=2), encoding="utf-8")


def project_colors():
    """프로젝트별 색 {이름: 색}. 한 번 배정한 색은 볼트의 색상 파일에 고정해 다른 프로젝트가 생겨도 안 바뀐다.
    새 프로젝트는 현재 안 쓰는 색 중 첫 번째, 노트가 하나도 없는 프로젝트의 색은 반납된다."""
    names = {t["project"] for t in list_tasks(include_archived=True)}
    with lock:
        saved = _load_colors()
        if saved is None:  # 이전 버전은 config.json에 저장했다 → 한 번만 옮긴다
            cfg = load_config()
            saved = cfg.pop("project_colors", None) or {}
            if "project_colors" in load_config():
                save_config(cfg)
        colors = {k: v for k, v in saved.items() if k in names}
        for n in sorted(names - set(colors)):
            free = [c for c in PALETTE if c not in colors.values()]
            colors[n] = free[0] if free else PALETTE[len(colors) % len(PALETTE)]
        if colors != _load_colors():
            _save_colors(colors)
    return colors


def create_task(data):
    title = (data.get("title") or "").strip()
    if not title:
        raise ValueError("업무 이름이 필요해요")
    project = (data.get("project") or "기타").strip()
    if project in archived_projects():  # 보관된 프로젝트에 업무를 추가하면 다시 꺼낸다
        set_archived(project, False)
    name = re.sub(r'[\\/:*?"<>|#^\[\]]', " ", f"{project} - {title}").strip()
    d = tasks_dir()
    d.mkdir(parents=True, exist_ok=True)
    p, i = d / f"{name}.md", 2
    while p.exists():
        p, i = d / f"{name} ({i}).md", i + 1
    today = date.today().isoformat()
    checks = [c.strip() for c in (data.get("checks") or "").splitlines() if c.strip()]
    check_lines = "\n".join(f"- [ ] {c}" for c in checks) or "- [ ] "
    p.write_text(
        f"""---
type: 업무
project: {project}
status: {data.get('status') or '할일'}
priority: {data.get('priority') or '보통'}
due: {data.get('due') or ''}
created: {today}
people: []
tags: [work, 업무]
---

# {p.stem}

## 할 일
{check_lines}

## 진행 기록
- {today}: 등록 (업무보드)
""",
        encoding="utf-8",
    )
    return task_from_file(p)


# ---------- 구글 캘린더 (iCal 비공개 주소) ----------

_ics_cache = {}  # url -> (fetched_at, Calendar)


def fetch_calendar(src):
    hit = _ics_cache.get(src)
    if hit and time.time() - hit[0] < 300:
        return hit[1]
    if src.startswith(("http://", "https://", "webcal://")):
        url = src.replace("webcal://", "https://", 1)
        req = urllib.request.Request(url, headers={"User-Agent": "work-board"})
        with urllib.request.urlopen(req, timeout=15) as r:
            raw = r.read()
    else:
        raw = Path(src).read_bytes()
    cal = icalendar.Calendar.from_ical(raw)
    _ics_cache[src] = (time.time(), cal)
    return cal


def to_iso(v):
    if isinstance(v, datetime):
        if v.tzinfo:
            v = v.astimezone()  # PC 로컬 시간대로
        return v.replace(tzinfo=None).isoformat(timespec="minutes")
    return v.isoformat()


HOLIDAY_ICS = "https://calendar.google.com/calendar/ical/ko.south_korea%23holiday%40group.v.calendar.google.com/public/basic.ics"
HOLIDAY_CACHE = ROOT / "holidays_cache.ics"
_holiday_mem = {"at": 0, "cal": None}


def holiday_calendar():
    """구글 '대한민국의 휴일' 공개 캘린더. 하루 한 번 받아서 파일로 보관 (인터넷이 안 되면 보관본 사용)."""
    if _holiday_mem["cal"] is not None and time.time() - _holiday_mem["at"] < 86400:
        return _holiday_mem["cal"]
    raw = None
    try:
        if not HOLIDAY_CACHE.exists() or time.time() - HOLIDAY_CACHE.stat().st_mtime > 86400:
            req = urllib.request.Request(HOLIDAY_ICS, headers={"User-Agent": "work-board"})
            with urllib.request.urlopen(req, timeout=15) as r:
                raw = r.read()
            HOLIDAY_CACHE.write_bytes(raw)
    except Exception as e:
        print("공휴일 받기 실패:", e)
    if raw is None and HOLIDAY_CACHE.exists():
        raw = HOLIDAY_CACHE.read_bytes()
    cal = icalendar.Calendar.from_ical(raw) if raw else None
    _holiday_mem.update(at=time.time(), cal=cal)
    return cal


def list_holidays(start, end):
    cal = holiday_calendar()
    out = {}
    if cal is None:
        return out
    for ev in recurring_ical_events.of(cal).between(start, end):
        s = ev.get("DTSTART").dt
        e = ev.get("DTEND").dt if ev.get("DTEND") else s + timedelta(days=1)
        name = str(ev.get("SUMMARY", "")).strip()
        kind = "holiday" if "공휴일" in str(ev.get("DESCRIPTION", "")) else "observance"
        if name.startswith("쉬는 날 "):
            name = "대체공휴일"
        d = s if isinstance(s, date) and not isinstance(s, datetime) else s.date()
        last = e if isinstance(e, date) and not isinstance(e, datetime) else e.date()
        while d < last:
            iso = d.isoformat()
            # 같은 날 공휴일과 기념일이 겹치면 공휴일 우선
            if iso not in out or (kind == "holiday" and out[iso]["kind"] != "holiday"):
                out[iso] = {"name": name, "kind": kind}
            d += timedelta(days=1)
    return out


def is_holiday(d):
    try:
        h = list_holidays(d, d + timedelta(days=1)).get(d.isoformat())
        return bool(h and h["kind"] == "holiday")
    except Exception:
        return False


def is_last_workday(d):
    """이번 주 마지막 근무일인지 (금요일이 공휴일이면 목요일)."""
    if d.weekday() > 4 or is_holiday(d):
        return False
    return all(is_holiday(d + timedelta(days=i)) for i in range(1, 5 - d.weekday()))


def list_events(start, end):
    events, errors = [], []
    for idx, src in enumerate(load_config()["ics_urls"]):
        try:
            cal = fetch_calendar(src)
            cal_name = str(cal.get("X-WR-CALNAME", f"캘린더 {idx + 1}"))
            for ev in recurring_ical_events.of(cal).between(start, end):
                s = ev.get("DTSTART").dt
                e = ev.get("DTEND").dt if ev.get("DTEND") else s
                events.append({
                    "title": str(ev.get("SUMMARY", "(제목 없음)")),
                    "start": to_iso(s),
                    "end": to_iso(e),
                    "allDay": not isinstance(s, datetime),
                    "location": str(ev.get("LOCATION", "") or ""),
                    "description": str(ev.get("DESCRIPTION", "") or "")[:500],
                    "calendar": cal_name,
                    "calIndex": idx,
                })
        except Exception as e:
            errors.append(f"캘린더 {idx + 1}: {e}")
    return {"events": events, "errors": errors}


# ---------- 알림 (Windows 토스트) ----------

TOAST_PS = r"""
$ErrorActionPreference = 'Stop'
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] | Out-Null
$xml = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($env:WB_TOAST_XML))
$doc = New-Object Windows.Data.Xml.Dom.XmlDocument
$doc.LoadXml($xml)
$appId = '{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe'
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($appId).Show([Windows.UI.Notifications.ToastNotification]::new($doc))
"""


def show_toast(title, lines):
    import subprocess
    from xml.sax.saxutils import escape
    url = f"http://127.0.0.1:{load_config()['port']}/"
    texts = "".join(f"<text>{escape(t)}</text>" for t in [title] + [l for l in lines if l][:2])
    xml = (f'<toast activationType="protocol" launch="{escape(url)}" scenario="reminder">'
           f'<visual><binding template="ToastGeneric">{texts}</binding></visual>'
           f'<actions><action content="업무보드 열기" activationType="protocol" arguments="{escape(url)}"/>'
           f'<action content="닫기" activationType="system" arguments="dismiss"/></actions></toast>')
    import base64
    import os
    env = dict(os.environ, WB_TOAST_XML=base64.b64encode(xml.encode("utf-8")).decode("ascii"))
    r = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", TOAST_PS],
        env=env, capture_output=True, timeout=30,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if r.returncode != 0:
        raise RuntimeError("알림 표시 실패: " + r.stderr.decode("cp949", "ignore")[:300])


def short(t):
    return t["title"].replace(t["project"] + " - ", "")


def open_issue_count():
    d = Path(load_config()["work_dir"]) / "이슈"
    n = 0
    for p in d.glob("*.md") if d.exists() else []:
        try:
            if read_note(p)[0].get("status") in ("미해결", "확인필요"):
                n += 1
        except Exception:
            pass
    return n


def build_alarm(kind):
    """알림 내용 (title, lines). 보낼 게 없으면 None."""
    today = date.today().isoformat()
    tomorrow = (date.today() + timedelta(days=1)).isoformat()
    personal = personal_projects()  # 개인 프로젝트는 업무 알림에서 뺀다
    open_ = [t for t in list_tasks() if t["status"] not in ("완료", "보류") and t["project"] not in personal]
    late = sorted([t for t in open_ if t["due"] and t["due"] < today], key=lambda t: t["due"])
    due_today = [t for t in open_ if t["due"] == today]
    if kind == "morning":
        evs = [e for e in list_events(date.today(), date.today() + timedelta(days=1))["events"]
               if e["start"][:10] <= today]
        evs.sort(key=lambda e: e["start"])
        due_tom = [t for t in open_ if t["due"] == tomorrow]
        issues = open_issue_count()
        head = f"지연 {len(late)} · 오늘 마감 {len(due_today)} · 내일 마감 {len(due_tom)} · 일정 {len(evs)}"
        lines = []
        if evs:
            lines.append("📅 " + ", ".join(("종일 " if e["allDay"] else e["start"][11:16] + " ") + e["title"] for e in evs[:3]))
        focus = due_today + [t for t in late if t["priority"] == "높음"] + late
        seen, names = set(), []
        for t in focus:
            if t["id"] not in seen:
                seen.add(t["id"]); names.append(short(t))
        if names:
            lines.append("✅ " + ", ".join(names[:3]) + (f" 외 {len(names) - 3}건" if len(names) > 3 else ""))
        if issues:
            lines[-1:] = [(lines[-1] + " " if lines else "") + f"· 확인할 이슈 {issues}건"]
        return f"좋은 아침이에요 ☀ {head}", lines or ["오늘 마감인 업무도, 일정도 없어요."]
    if kind == "evening":
        if not due_today and not late:
            return None
        lines = []
        if due_today:
            lines.append("오늘 마감: " + ", ".join(short(t) for t in due_today[:3]))
        if late:
            lines.append(f"지연 {len(late)}건: " + ", ".join(short(t) for t in late[:2]) + (" …" if len(late) > 2 else ""))
        return f"퇴근 전 체크 — 못 끝낸 업무 {len(due_today) + len(late)}건", lines
    if kind in ("review_week", "review_month"):
        word = "이번 주" if kind == "review_week" else "이번 달"
        g = list_growth()
        per = (date.today().strftime("%G-W%V") if kind == "review_week" else date.today().strftime("%Y-%m"))
        want = "주간" if kind == "review_week" else "월간"
        done = any(r["kind"] == want and r["period"] == per for r in g["reviews"])
        lines = [f"{word}{'를' if kind == 'review_week' else '을'} 돌아볼 시간이에요. 잘한 점 · 아쉬운 점 · 다음 다짐을 1분만 적어 보세요 (🌱 성장 탭)"]
        if done:
            return None  # 이미 썼으면 조용히 넘어간다
        if late:
            lines.append(f"참고: 지연된 업무 {len(late)}건")
        return f"📝 {word} 회고 시간", lines
    if kind == "friday":
        monday = (date.today() - timedelta(days=date.today().weekday())).isoformat()
        done = [t for t in list_tasks() if t["status"] == "완료" and (t["completed"] or "") >= monday]
        return "금요일 정기 업무 🔁", [
            "소스 정리 후 NAS 업데이트, 주간 회고 작성",
            f"이번 주 완료 {len(done)}건 · 남은 지연 {len(late)}건",
        ]
    return None


def load_state():
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


# 알림별 보낼 수 있는 시간 범위: (시작 = 설정 시각, 이 시각까지만). PC를 늦게 켜도 범위 안이면 한 번 보낸다.
ALARM_UNTIL = {"morning": "13:00", "evening": "21:00", "friday": "21:00", "review_week": "21:00", "review_month": "21:00"}


def alarm_loop():
    while True:
        try:
            now = datetime.now()
            hm, today = now.strftime("%H:%M"), now.date().isoformat()
            state = load_state()
            for kind, at in load_config().get("alarms", {}).items():
                if not at or kind not in ALARM_UNTIL or state.get(kind) == today:
                    continue
                if kind in ("friday", "review_week") and not is_last_workday(now.date()):
                    continue
                if kind == "review_month" and not is_last_workday_of_month(now.date()):
                    continue
                if kind not in ("friday", "review_week", "review_month") and (now.weekday() >= 5 or is_holiday(now.date())):  # 주말·공휴일엔 쉼
                    continue
                if at <= hm <= ALARM_UNTIL[kind]:
                    # 먼저 기록해 둬야 알림이 실패해도 30초마다 반복되지 않는다
                    state[kind] = today
                    STATE_PATH.write_text(json.dumps(state), encoding="utf-8")
                    msg = build_alarm(kind)
                    if msg:
                        show_toast(*msg)
        except Exception as e:
            print("알림 오류:", e)
        time.sleep(30)


# ---------- 성장(회고·목표·선물) ----------

GROWTH_DIR_NAME = "성장"
REVIEW_KINDS = ("주간", "월간", "프로젝트")


def growth_dir():
    return Path(load_config()["work_dir"]) / GROWTH_DIR_NAME


def personal_projects():
    return set(load_config().get("personal_projects") or [])


def _sections(body):
    out, cur = {}, None
    for line in body.splitlines():
        if line.startswith("## "):
            cur = line[3:].strip()
            out[cur] = []
        elif cur is not None:
            out[cur].append(line)
    return {k: "\n".join(v).strip() for k, v in out.items()}


def _safe_text(s):
    """본문 사용자 입력이 '## ' 제목처럼 읽히지 않도록 줄 맨 앞의 #을 바꾼다."""
    return re.sub(r"(?m)^#", "＃", (s or "").strip())


def _bool(v):
    return str(v).strip().lower() == "true"


def review_from_file(p):
    fm, body, _ = read_note(p)
    if fm.get("type") != "회고":
        return None
    sec = _sections(body)
    try:
        rating = int(fm.get("rating") or 0)
    except ValueError:
        rating = 0
    return {
        "id": p.name, "kind": fm.get("kind") or "주간", "period": fm.get("period") or "",
        "project": fm.get("project") or "", "rating": rating, "created": fm.get("created") or "",
        "good": sec.get("잘한 점", ""), "bad": sec.get("아쉬운 점·부족한 부분", ""), "next": sec.get("다음 다짐", ""),
        "mtime": p.stat().st_mtime,
    }


def goal_from_file(p):
    fm, body, _ = read_note(p)
    if fm.get("type") != "목표":
        return None
    title = p.stem
    for line in body.splitlines():
        if line.startswith("# "):
            title = line[2:].strip()
            break
    return {
        "id": p.name, "title": title, "status": fm.get("status") or "진행중", "project": fm.get("project") or "",
        "reward": fm.get("reward") or "", "due": fm.get("due") or None, "created": fm.get("created") or "",
        "achieved": fm.get("achieved") or None, "reward_claimed": _bool(fm.get("reward_claimed")),
        "mtime": p.stat().st_mtime,
    }


def list_growth():
    d = growth_dir()
    reviews, goals = [], []
    if d.exists():
        for p in sorted(d.glob("*.md")):
            try:
                r = review_from_file(p)
                if r:
                    reviews.append(r)
                    continue
                g = goal_from_file(p)
                if g:
                    goals.append(g)
            except Exception as e:
                print("성장 노트 읽기 실패:", p.name, e)
    reviews.sort(key=lambda r: (r["created"], r["mtime"]), reverse=True)
    goals.sort(key=lambda g: (g["status"] == "달성", -g["mtime"]))
    return {"reviews": reviews, "goals": goals, "personal": sorted(personal_projects()), "today": date.today().isoformat()}


def _growth_path(gid):
    p = (growth_dir() / gid).resolve()
    if p.parent != growth_dir().resolve() or p.suffix != ".md" or not p.exists():
        raise ValueError("잘못된 id")
    return p


def _clean_name(s):
    return re.sub(r'[\\/:*?"<>|#^\[\]]', " ", s).strip()


def save_review(data):
    kind = (data.get("kind") or "주간").strip()
    if kind not in REVIEW_KINDS:
        raise ValueError("회고 종류가 올바르지 않아요")
    period = (data.get("period") or "").strip()
    if not period:
        raise ValueError("기간을 입력해 주세요")
    project = (data.get("project") or "").strip()
    try:
        rating = max(0, min(5, int(data.get("rating") or 0)))
    except (TypeError, ValueError):
        rating = 0
    good, bad, nxt = _safe_text(data.get("good")), _safe_text(data.get("bad")), _safe_text(data.get("next"))
    if not (good or bad or nxt):
        raise ValueError("잘한 점, 아쉬운 점, 다음 다짐 중 하나는 적어 주세요")
    name = _clean_name(f"회고 - {period}" + (f" - {project}" if project else ""))
    d = growth_dir()
    d.mkdir(parents=True, exist_ok=True)
    with lock:
        p = d / f"{name}.md"
        created = date.today().isoformat()
        if p.exists():  # 같은 기간·프로젝트 회고는 덮어써서 고친다 (처음 작성일은 유지)
            old = read_note(p)[0]
            created = old.get("created") or created
        text = f"""---
type: 회고
kind: {kind}
period: {period}
project: {project}
rating: {rating}
created: {created}
tags: [work, 회고]
---

# {name}

## 잘한 점
{good}

## 아쉬운 점·부족한 부분
{bad}

## 다음 다짐
{nxt}
"""
        p.write_text(text, encoding="utf-8", newline="")
    return review_from_file(p)


def create_goal(data):
    title = (data.get("title") or "").strip()
    if not title:
        raise ValueError("목표를 입력해 주세요")
    d = growth_dir()
    d.mkdir(parents=True, exist_ok=True)
    name = _clean_name(f"목표 - {title}")
    with lock:
        p, i = d / f"{name}.md", 2
        while p.exists():
            p, i = d / f"{name} ({i}).md", i + 1
        text = f"""---
type: 목표
status: 진행중
project: {(data.get('project') or '').strip()}
reward: {(data.get('reward') or '').strip()}
due: {(data.get('due') or '').strip()}
created: {date.today().isoformat()}
achieved:
reward_claimed: false
tags: [work, 목표]
---

# {title}
"""
        p.write_text(text, encoding="utf-8", newline="")
    return goal_from_file(p)


def update_goal(data):
    with lock:
        p = _growth_path(data["id"])
        text = p.read_text(encoding="utf-8")
        if "status" in data:
            if data["status"] not in ("진행중", "달성"):
                raise ValueError("잘못된 상태")
            text = set_frontmatter(text, "status", data["status"])
            text = set_frontmatter(text, "achieved", date.today().isoformat() if data["status"] == "달성" else "")
            if data["status"] != "달성":
                text = set_frontmatter(text, "reward_claimed", "false")
        if "reward_claimed" in data:
            text = set_frontmatter(text, "reward_claimed", "true" if data["reward_claimed"] else "false")
        if "reward" in data:
            text = set_frontmatter(text, "reward", (data["reward"] or "").strip())
        if "due" in data:
            text = set_frontmatter(text, "due", (data["due"] or "").strip())
        p.write_text(text, encoding="utf-8", newline="")
    return goal_from_file(p)


def delete_growth(data):
    """회고·목표 노트를 지우지 않고 Work/_휴지통으로 옮긴다."""
    with lock:
        p = _growth_path(data["id"])
        trash = Path(load_config()["work_dir"]) / "_휴지통"
        trash.mkdir(exist_ok=True)
        dest = trash / p.name
        if dest.exists():
            dest = trash / f"{p.stem} ({datetime.now():%Y%m%d-%H%M%S}){p.suffix}"
        p.rename(dest)
    return {"id": data["id"]}


def is_last_workday_of_month(d):
    if d.weekday() > 4 or is_holiday(d):
        return False
    n = d + timedelta(days=1)
    while n.month == d.month:
        if n.weekday() <= 4 and not is_holiday(n):
            return False
        n += timedelta(days=1)
    return True


# ---------- 루틴 달력 (매일 체크하는 습관) ----------

ROUTINES_FILE = "루틴.json"


def _routines_path():
    return growth_dir() / ROUTINES_FILE


def load_routines():
    try:
        data = json.loads(_routines_path().read_text(encoding="utf-8"))
    except Exception:
        data = {}
    if not isinstance(data, dict):
        data = {}
    data.setdefault("routines", [])
    data.setdefault("checks", {})
    return data


def _save_routines(data):
    p = _routines_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(p)


def set_routine_check(data):
    rid, d, done = str(data.get("routine") or ""), str(data.get("date") or ""), bool(data.get("done"))
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", d):
        raise ValueError("날짜 형식은 YYYY-MM-DD")
    day = int(d[8:])
    with lock:
        doc = load_routines()
        if not any(r.get("id") == rid for r in doc["routines"]):
            raise ValueError("없는 루틴이에요. 새로고침 후 다시 해 주세요")
        days = set(doc["checks"].setdefault(d[:7], {}).get(rid, []))
        days.add(day) if done else days.discard(day)
        doc["checks"][d[:7]][rid] = sorted(days)
        _save_routines(doc)
    return {"ok": True}


def save_routine_list(data):
    import uuid
    items, out, seen = data.get("routines") or [], [], set()
    for it in items:
        name = (it.get("name") or "").strip()
        if not name:
            continue
        rid = str(it.get("id") or "")
        if not rid or rid in seen:
            rid = "r" + uuid.uuid4().hex[:8]
        seen.add(rid)
        out.append({"id": rid, "name": name[:40], "emoji": (it.get("emoji") or "").strip()[:4]})
    with lock:
        doc = load_routines()
        doc["routines"] = out
        _save_routines(doc)
    return {"routines": out}


# ---------- HTTP ----------

class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def send_json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def read_json(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    def do_GET(self):
        u = urlparse(self.path)
        try:
            if u.path in ("/", "/index.html"):
                body = (ROOT / "index.html").read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif u.path == "/api/tasks":
                self.send_json({"tasks": list_tasks(), "archived": archived_summary(), "colors": project_colors(),
                                "personal": sorted(personal_projects()), "today": date.today().isoformat()})
            elif u.path == "/api/growth":
                self.send_json(list_growth())
            elif u.path == "/api/routines":
                self.send_json(load_routines())
            elif u.path == "/api/events":
                q = parse_qs(u.query)
                start = date.fromisoformat(q.get("start", [date.today().isoformat()])[0][:10])
                end = date.fromisoformat(q.get("end", [(date.today() + timedelta(days=42)).isoformat()])[0][:10])
                self.send_json(list_events(start, end))
            elif u.path == "/api/holidays":
                q = parse_qs(u.query)
                self.send_json(list_holidays(date.fromisoformat(q["start"][0][:10]), date.fromisoformat(q["end"][0][:10])))
            elif u.path == "/api/config":
                cfg = load_config()
                self.send_json({"ics_count": len(cfg["ics_urls"]), "work_dir": cfg["work_dir"], "alarms": cfg.get("alarms", {})})
            else:
                self.send_json({"error": "없는 주소"}, 404)
        except Exception as e:
            self.send_json({"error": str(e)}, 500)

    def do_POST(self):
        u = urlparse(self.path)
        try:
            data = self.read_json()
            if u.path == "/api/task/update":
                self.send_json({"task": update_task(data)})
            elif u.path == "/api/routines/check":
                self.send_json(set_routine_check(data))
            elif u.path == "/api/routines/list":
                self.send_json(save_routine_list(data))
            elif u.path == "/api/growth/review":
                self.send_json({"review": save_review(data)})
            elif u.path == "/api/growth/goal":
                self.send_json({"goal": create_goal(data)})
            elif u.path == "/api/growth/goal/update":
                self.send_json({"goal": update_goal(data)})
            elif u.path == "/api/growth/delete":
                self.send_json(delete_growth(data))
            elif u.path == "/api/config/personal":
                cfg = load_config()
                cfg["personal_projects"] = [s.strip() for s in data.get("projects", []) if s.strip()]
                save_config(cfg)
                self.send_json({"personal": cfg["personal_projects"]})
            elif u.path == "/api/project/rename":
                self.send_json(rename_project(data.get("from"), data.get("to")))
            elif u.path == "/api/project/archive":
                set_archived(data.get("project"), bool(data.get("archived", True)))
                self.send_json({"ok": True})
            elif u.path == "/api/task/delete":
                self.send_json(delete_task(data))
            elif u.path == "/api/task/create":
                self.send_json({"task": create_task(data)})
            elif u.path == "/api/config/ics":
                cfg = load_config()
                urls = [s.strip() for s in data.get("ics_urls", []) if s.strip()]
                cfg["ics_urls"] = urls
                save_config(cfg)
                _ics_cache.clear()
                self.send_json({"ics_count": len(urls)})
            elif u.path == "/api/config/alarms":
                cfg = load_config()
                alarms = {}
                for k in ALARM_UNTIL:
                    v = (data.get(k) or "").strip()
                    if v and not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", v):
                        raise ValueError("시각은 HH:MM 형식")
                    alarms[k] = v
                cfg["alarms"] = alarms
                save_config(cfg)
                self.send_json({"alarms": alarms})
            elif u.path == "/api/notify/test":
                msg = build_alarm(data.get("kind", "morning")) or ("보낼 알림이 없어요", ["오늘 마감·지연 업무가 없어요."])
                show_toast(*msg)
                self.send_json({"title": msg[0], "lines": msg[1]})
            else:
                self.send_json({"error": "없는 주소"}, 404)
        except ValueError as e:
            self.send_json({"error": str(e)}, 400)
        except Exception as e:
            self.send_json({"error": str(e)}, 500)


def main():
    cfg = load_config()
    if not CONFIG_PATH.exists():
        save_config(cfg)
    port = int(cfg["port"])
    try:
        srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    except OSError:  # 이미 실행 중이면 브라우저만 연다
        webbrowser.open(f"http://127.0.0.1:{port}")
        return
    threading.Thread(target=alarm_loop, daemon=True).start()
    print(f"업무보드 실행 중: http://127.0.0.1:{port}  (끄려면 이 창을 닫으세요)")
    if "--no-browser" not in __import__("sys").argv:
        threading.Timer(0.8, lambda: webbrowser.open(f"http://127.0.0.1:{port}")).start()
    srv.serve_forever()


if __name__ == "__main__":
    main()
