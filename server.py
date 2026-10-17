"""콕핏(구 업무보드): Obsidian Work/업무 노트 + 구글 캘린더(iCal)를 달력·카드로 보여주는 로컬 웹 서버.

실행: python server.py  →  http://127.0.0.1:8765
데이터는 Obsidian 노트가 원본이다. 이 서버는 노트를 읽고, 상태·마감·체크리스트·진행 기록만 고쳐 쓴다.
"""
import calendar
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
CHART_BOARD = "차트"          # 노트 머리말 board: 차트 → 차트보드 종목 카드 (업무 알림·주간 보고·정기 업무에서 빠진다)
CHART_MARKETS = ("한국", "미국")  # 차트보드의 우선순위 자리: 시장 구분
CHART_ORIGINS = ("https://oldhill-kr.github.io", "http://127.0.0.1", "http://localhost")  # 군영 대시보드에서 체크·메모를 보낼 수 있는 곳
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


def split_themes(v):
    """'로봇, 자동차' 또는 목록 → ['로봇', '자동차'] (공백·중복 제거). 한 종목이 여러 테마에 속할 수 있다."""
    items = v if isinstance(v, (list, tuple)) else re.split(r"[,，/·]", str(v or ""))
    out = []
    for x in items:
        x = re.sub(r'[\\/:*?"<>|#^\[\]]', " ", str(x)).strip()
        if x and x not in out:
            out.append(x)
    return out


def _themes_of(fm):
    th = split_themes(fm.get("themes") or [])
    proj = fm.get("project") or "기타"
    return th if th and th[0] == proj else [proj] + [x for x in th if x != proj]


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
        "priority": fm.get("priority") or ("한국" if fm.get("board") == CHART_BOARD else "보통"),
        "board": fm.get("board") or "업무",
        "symbol": str(fm.get("symbol") or ""),
        "themes": _themes_of(fm),
        "due": fm.get("due") or None,
        "created": fm.get("created") or None,
        "completed": fm.get("completed") or None,
        "archived": str(fm.get("archived", "")).lower() == "true",
        "recurring": fm.get("recurring") or "",
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


def list_tasks(include_archived=False, charts=False):
    """업무 카드. 차트보드 종목 카드(board: 차트)는 charts=True일 때만 포함한다 (알림·보고서는 기본값으로 뺀다)."""
    d = tasks_dir()
    if not d.exists():
        return []
    out = []
    for p in sorted(d.glob("*.md")):
        try:
            t = task_from_file(p)
            if t and (include_archived or not t["archived"]) and (charts or t["board"] != CHART_BOARD):
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


def reorder_check_line(text, frm, to):
    """frm번째 체크박스 줄을 to번째 자리로 옮긴다 (체크 줄끼리만 자리를 바꾸고, 그 사이의 다른 줄은 그대로)."""
    nl = "\r\n" if "\r\n" in text else "\n"
    lines = text.split(nl)
    pos = [i for i, l in enumerate(lines) if CHECK_RE.match(l)]
    if not (0 <= frm < len(pos) and 0 <= to < len(pos)):
        raise ValueError("할 일 항목을 찾지 못했어요. 새로고침 후 다시 해 주세요")
    if frm == to:
        return text
    items = [lines[p] for p in pos]
    items.insert(to, items.pop(frm))
    for p, l in zip(pos, items):
        lines[p] = l
    return nl.join(lines)


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


def add_check_line(text, item, done=False):
    """'## 할 일' 섹션의 마지막 체크박스 뒤에 추가 (빈 체크박스가 있으면 그 자리에)."""
    nl = "\r\n" if "\r\n" in text else "\n"
    lines = text.split(nl)
    new = f"- [{'x' if done else ' '}] {item}"
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


def rename_task(task_id, new_title):
    """업무 이름만 바꾼다. 파일 이름은 '프로젝트 - 이름', H1 제목과 Work 안의 [[링크]]도 함께 고친다. 새 id(파일명)를 돌려준다."""
    new_title = (new_title or "").strip()
    if not new_title:
        raise ValueError("업무 이름이 필요해요")
    if BAD_NAME_RE.search(new_title):
        raise ValueError('이름에 \\ / : * ? " < > | # ^ [ ] 는 쓸 수 없어요')
    d = tasks_dir()
    with lock:
        p = safe_task_path(task_id)
        fm, _, text = read_note(p)
        project = fm.get("project") or "기타"
        stem = p.stem
        new_stem = f"{project} - {new_title}"
        if new_stem == stem:
            return p.name
        dest, i = d / f"{new_stem}.md", 2
        while dest.exists() and dest != p:
            dest, i = d / f"{new_stem} ({i}).md", i + 1
        new_stem = dest.stem
        if re.search(rf"^# {re.escape(stem)}\s*$", text, flags=re.M):
            text = re.sub(rf"^# {re.escape(stem)}\s*$", lambda m: f"# {new_stem}", text, count=1, flags=re.M)
        p.write_text(text, encoding="utf-8", newline="")
        p.rename(dest)
        _fix_links({stem: new_stem})
    return dest.name


def _oneline(s):
    """여러 줄 입력은 글머리 한 줄을 유지하도록 <br>로 잇는다."""
    return re.sub(r"[ \t]*\r?\n[ \t]*", "<br>", (s or "").strip())


def move_check(task_id, mv):
    """다른 업무 노트로 할 일 하나를 옮긴다 (프로젝트가 달라도 됨) (체크 상태·🔥 긴급 표시 유지). 원래 업무를 돌려준다."""
    idxs = sorted({int(i) for i in (mv.get("idxs") if mv.get("idxs") is not None else [mv.get("idx")])})
    to = (mv.get("to") or "").strip()
    if not to or to == task_id:
        raise ValueError("옮길 업무를 골라 주세요")
    with lock:
        sp, tp = safe_task_path(task_id), safe_task_path(to)
        sfm, _, stext = read_note(sp)
        tfm, _, ttext = read_note(tp)
        n, found = -1, {}
        for line in stext.split("\r\n" if "\r\n" in stext else "\n"):
            cm = CHECK_RE.match(line)
            if cm:
                n += 1
                if n in idxs:
                    found[n] = (cm.group(4), cm.group(2) != " ")
        if len(found) != len(idxs) or not idxs:
            raise ValueError("할 일 항목을 찾지 못했어요. 새로고침 후 다시 해 주세요")
        new_t, new_s = ttext, stext
        for i in idxs:  # 받는 쪽에는 원래 순서대로 붙인다
            new_t = add_check_line(new_t, found[i][0], found[i][1])
        for i in reversed(idxs):  # 지울 때는 뒤에서부터 (앞 번호가 안 밀리게)
            new_s = edit_check_line(new_s, i, None)
        tp.write_text(new_t, encoding="utf-8", newline="")  # 받는 쪽을 먼저 써서 중간에 실패해도 할 일이 사라지지 않게
        sp.write_text(new_s, encoding="utf-8", newline="")
    return task_from_file(sp)


def update_task(data):
    if data.get("move_check") is not None:
        return move_check(data["id"], data["move_check"])
    if data.get("new_title") is not None:
        data = dict(data)
        data["id"] = rename_task(data["id"], data.pop("new_title"))
    if data.get("themes") is not None:  # 차트 카드: 테마 여러 개 (첫 번째가 대표)
        data = dict(data)
        data["id"] = set_card_themes(data["id"], data.pop("themes"))["id"]
    if data.get("project") is not None:  # 프로젝트 변경은 파일 이름이 바뀌므로 먼저 처리하고 새 id로 이어간다
        data = dict(data)
        data["id"] = change_task_project(data["id"], data.pop("project"))
    p = safe_task_path(data["id"])
    with lock:
        text = p.read_text(encoding="utf-8")
        if "status" in data:
            if data["status"] not in STATUSES:
                raise ValueError("잘못된 상태")
            text = set_frontmatter(text, "status", data["status"])
            if data["status"] == "완료":
                text = set_frontmatter(text, "completed", date.today().isoformat())
            else:
                text = remove_frontmatter(text, "completed")
        if "due" in data:
            due = data["due"] or ""
            if due and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", due):
                raise ValueError("마감일 형식은 YYYY-MM-DD")
            text = set_frontmatter(text, "due", due)
        if "priority" in data:
            text = set_frontmatter(text, "priority", data["priority"])
        if "set_view" in data:  # 차트보드: '## 관점' 메모를 통째로 바꾼다
            memo = str(data.get("set_view") or "").strip()
            text = _set_section_bullets(text, VIEW_SECTION, [memo] if memo else [])
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
        if data.get("reorder_check") is not None:
            rc = data["reorder_check"]
            text = reorder_check_line(text, int(rc["from"]), int(rc["to"]))
        if "delete_check" in data:
            text = edit_check_line(text, int(data["delete_check"]), None)
        if "edit_check" in data:
            new = (data.get("text") or "").strip()
            if not new:
                raise ValueError("내용을 입력해 주세요")
            text = edit_check_line(text, int(data["edit_check"]), _oneline(new))
        if data.get("add_check"):
            text = add_check_line(text, _oneline(data["add_check"]))
        if data.get("log"):
            text = append_log(text, _oneline(data["log"]))
        if data.get("add_note"):
            n = data["add_note"]
            sec, txt = (n.get("section") or "진행 기록").strip(), (n.get("text") or "").strip()
            if not txt:
                raise ValueError("내용을 입력해 주세요")
            text = append_log(text, _oneline(txt), sec, dated=(sec == "진행 기록"))
        if data.get("edit_note"):
            n = data["edit_note"]
            txt = (n.get("text") or "").strip()
            if not txt:
                raise ValueError("내용을 입력해 주세요")
            text = edit_note_line(text, (n.get("section") or "").strip(), int(n["idx"]), _oneline(txt))
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


def delete_project(project):
    """보관된 프로젝트의 업무 노트를 모두 Work/_휴지통으로 옮긴다 (옵시디언에서 되살릴 수 있음)."""
    project = (project or "").strip()
    if not project:
        raise ValueError("프로젝트 이름이 필요해요")
    if project not in archived_projects():
        raise ValueError("보관함에 있는 프로젝트만 삭제할 수 있어요")
    ids = [t["id"] for t in list_tasks(include_archived=True) if t["project"] == project]
    for i in ids:
        delete_task({"id": i})
    return {"count": len(ids)}


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
    names = {n for t in list_tasks(include_archived=True, charts=True) for n in t["themes"]}
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
    chart = data.get("board") == CHART_BOARD
    themes = split_themes(data.get("project")) if chart else []
    project = (themes[0] if themes else None) or ("미분류" if chart else (data.get("project") or "기타").strip())
    if chart and not data.get("priority"):
        data = dict(data, priority="한국")
    if not chart and project in archived_projects():  # 보관된 프로젝트에 업무를 추가하면 다시 꺼낸다
        set_archived(project, False)
    name = re.sub(r'[\\/:*?"<>|#^\[\]]', " ", f"{project} - {title}").strip()
    d = tasks_dir()
    d.mkdir(parents=True, exist_ok=True)
    p, i = d / f"{name}.md", 2
    while p.exists():
        p, i = d / f"{name} ({i}).md", i + 1
    today = date.today().isoformat()
    checks = [c.strip() for c in (data.get("checks") or "").splitlines() if c.strip()]
    check_lines = "\n".join(f"- [ ] {c}" for c in checks) or ("" if chart else "- [ ] ")
    rec = (data.get("recurring") or "").strip()  # 정기 업무 규칙이 만든 노트는 규칙 id를 머리말에 남긴다
    rec_line = f"recurring: {rec}\n" if rec else ""
    board_line = f"board: {CHART_BOARD}\n" + (f"themes: [{', '.join(themes)}]\n" if len(themes) > 1 else "") if chart else ""
    symbol = re.sub(r"[^0-9A-Za-z.\-]", "", str(data.get("symbol") or ""))
    symbol_line = f"symbol: {symbol}\n" if symbol else ""
    tags = "[work, 차트]" if chart else "[work, 업무]"
    p.write_text(
        f"""---
type: 업무
project: {project}
status: {data.get('status') or '할일'}
priority: {data.get('priority') or '보통'}
due: {data.get('due') or ''}
created: {today}
{board_line}{symbol_line}people: []
{rec_line}tags: {tags}
---

# {p.stem}

## 할 일
{check_lines}

## 진행 기록
""",
        encoding="utf-8",
    )
    return task_from_file(p)

# ---------- 차트보드: 척후 보고의 체크·관점 메모 ----------

VIEW_SECTION = "관점"


def _set_section_bullets(text, section, items):
    """'## <섹션>'의 글머리 줄을 items로 통째로 바꾼다 (없으면 만든다). 줄바꿈은 <br>로 한 줄에 잇는다."""
    nl = "\r\n" if "\r\n" in text else "\n"
    lines = text.split(nl)
    new = [f"- {_oneline(i)}" for i in items if (i or "").strip()]
    try:
        start = next(i for i, l in enumerate(lines) if l.strip() == f"## {section}")
    except StopIteration:
        if not new:
            return text
        return text.rstrip() + nl + nl + f"## {section}" + nl + nl.join(new) + nl
    end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("## ")), len(lines))
    tail = [""] if end < len(lines) else []
    lines[start + 1:end] = new + tail
    return nl.join(lines)


def set_card_themes(task_id, themes):
    """차트 카드의 테마 목록을 바꾼다. 첫 번째가 대표 테마(파일 이름 접두어), 나머지는 머리말 themes에만 둔다."""
    themes = split_themes(themes) or ["미분류"]
    new_id = change_task_project(task_id, themes[0])
    p = safe_task_path(new_id)
    with lock:
        text = p.read_text(encoding="utf-8")
        text = set_frontmatter(text, "themes", "[" + ", ".join(themes) + "]") if len(themes) > 1 else remove_frontmatter(text, "themes")
        p.write_text(text, encoding="utf-8", newline="")
    return task_from_file(p)


def find_chart_card(symbol):
    for t in list_tasks(include_archived=True, charts=True):
        if t["board"] == CHART_BOARD and t["symbol"] == symbol:
            return t
    return None


def _chart_market(v):
    if v in CHART_MARKETS:
        return v
    return {"KR": "한국", "US": "미국"}.get(str(v or "").upper(), "한국")


def _memo_of(t):
    """카드 본문의 '## 관점' 글머리를 줄바꿈으로 이어 돌려준다."""
    head = "## " + VIEW_SECTION
    if head not in t["body"]:
        return ""
    block = t["body"].split(head, 1)[1].split("\n## ")[0]
    lines = [re.sub(r"^\s*[-*] ", "", l) for l in block.strip().splitlines() if l.strip()]
    return "\n".join(lines).replace("<br>", "\n")


def upsert_chart_scout(data):
    """군영 대시보드 척후 보고의 [차트 분석 체크]·[관점 메모]를 차트보드 카드에 반영한다.
    종목 코드로 카드를 찾고 없으면 만든다. 체크 → 상태 '완료'(분석 완료), 해제 → '할일'. 메모는 '## 관점' 섹션."""
    symbol = re.sub(r"[^0-9A-Za-z.\-]", "", str(data.get("symbol") or ""))
    if not symbol:
        raise ValueError("종목 코드가 필요해요")
    name = re.sub(r'[\\/:*?"<>|#^\[\]]', " ", str(data.get("name") or symbol)).strip() or symbol
    themes = split_themes(data.get("theme"))
    card = find_chart_card(symbol)
    if card is None:
        card = create_task({"board": CHART_BOARD, "title": name, "project": ", ".join(themes) or "미분류",
                            "priority": _chart_market(data.get("market")), "symbol": symbol})
    elif themes and themes != card["themes"]:
        card = set_card_themes(card["id"], themes)
    p = safe_task_path(card["id"])
    with lock:
        text = p.read_text(encoding="utf-8")
        if "checked" in data:
            old = read_note(p)[0].get("status") or "할일"
            if data["checked"] and old != "완료":
                text = set_frontmatter(text, "status", "완료")
                text = set_frontmatter(text, "completed", date.today().isoformat())
            elif not data["checked"] and old == "완료":
                text = set_frontmatter(text, "status", "할일")
                text = remove_frontmatter(text, "completed")
        if "memo" in data:
            memo = str(data.get("memo") or "").strip()
            text = _set_section_bullets(text, VIEW_SECTION, [memo] if memo else [])
        if data.get("scout"):  # 어떤 척후 보고에서 왔는지 한 줄 남긴다 (처음 한 번)
            sc = str(data["scout"])[:200]
            if sc not in text:
                text = append_log(text, f"척후 보고: {sc}", "배경", dated=False)
        p.write_text(text, encoding="utf-8", newline="")
    return task_from_file(p)


# ---------- 이슈 노트 (Work/이슈) ----------

ISSUE_DIR_NAME = "이슈"
OPEN_ISSUE_STATUS = ("미해결", "확인필요")


def list_issues():
    """Work/이슈의 노트를 가볍게 읽어 목록으로 (업무 상세의 '관련 이슈'와 주간 보고에 쓴다)."""
    d = Path(load_config()["work_dir"]) / ISSUE_DIR_NAME
    if not d.exists():
        return []
    vault = load_config()["vault_name"]
    out = []
    for p in d.glob("*.md"):
        try:
            fm, body, _ = read_note(p)
            title = p.stem
            for line in body.splitlines():
                if line.startswith("# "):
                    title = line[2:].strip()
                    break
            out.append({
                "id": p.name, "stem": p.stem, "title": title, "date": fm.get("date") or "",
                "status": fm.get("status") or "", "project": fm.get("project") or "",
                "category": fm.get("category") or "", "equipment": fm.get("equipment") or "",
                "mtime": p.stat().st_mtime,
                "obsidian": f"obsidian://open?vault={quote(vault)}&file={quote('Work/' + ISSUE_DIR_NAME + '/' + p.stem)}",
            })
        except Exception as e:
            print("이슈 노트 읽기 실패:", p.name, e)
    out.sort(key=lambda i: (i["date"], i["mtime"]), reverse=True)
    return out


# ---------- 대기 경과일 ----------

WAIT_LOG_RE = re.compile(r"^\s*[-*] (\d{4}-\d{2}-\d{2}): 상태 \S+ → 대기\s*$")


def wait_since(t):
    """'대기'로 바꾼 날짜 (진행 기록의 '상태 … → 대기' 줄). 기록이 없으면 노트를 마지막으로 고친 날."""
    last = None
    for line in t["body"].splitlines():
        m = WAIT_LOG_RE.match(line)
        if m:
            last = m.group(1)
    return last or datetime.fromtimestamp(t["mtime"]).date().isoformat()


# ---------- 정기(반복) 업무 ----------

RECURRING_FILE = ".업무보드-정기업무.json"  # 볼트에 둬서 웹 버전·다른 PC와 같은 규칙을 쓴다
WEEKDAY_KO = ["일", "월", "화", "수", "목", "금", "토"]  # 규칙의 요일 값은 자바스크립트와 같게 0=일 … 6=토


def _recurring_path():
    return Path(load_config()["work_dir"]) / RECURRING_FILE


def load_recurring():
    try:
        doc = json.loads(_recurring_path().read_text(encoding="utf-8"))
    except Exception:
        doc = {}
    items = doc.get("items") if isinstance(doc, dict) else None
    return {"items": items if isinstance(items, list) else []}


def _save_recurring(doc):
    p = _recurring_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(p)


def save_recurring_list(data):
    import uuid
    old = {r.get("id"): r for r in load_recurring()["items"]}
    out, seen = [], set()
    today = date.today().isoformat()
    for it in data.get("items") or []:
        title = (it.get("title") or "").strip()
        if not title:
            continue
        freq = it.get("freq") if it.get("freq") in ("weekly", "monthly") else "weekly"
        when = str(it.get("when", "")).strip()
        if when != "last":
            ok = when.isdigit() and ((freq == "weekly" and 0 <= int(when) <= 6) or (freq == "monthly" and 1 <= int(when) <= 31))
            if not ok:
                raise ValueError("요일 또는 날짜가 올바르지 않아요")
        rid = str(it.get("id") or "")
        if not rid or rid in seen:
            rid = "g" + uuid.uuid4().hex[:8]
        seen.add(rid)
        prev = old.get(rid) or {}
        pri = it.get("priority") if it.get("priority") in ("높음", "보통", "낮음") else "보통"
        out.append({
            "id": rid, "title": title[:80], "project": (it.get("project") or "").strip() or "기타",
            "priority": pri, "freq": freq, "when": when, "checks": (it.get("checks") or "").strip(),
            "since": prev.get("since") or today,  # 이 날짜 이후에 오는 발생일부터 만든다 (저장 전 날짜는 소급하지 않음)
            "last_period": prev.get("last_period") or "",
        })
    with lock:
        _save_recurring({"items": out})
    return {"items": out}


def _last_workday(start, end):
    d = end
    while d >= start:
        if d.weekday() <= 4 and not is_holiday(d):
            return d
        d -= timedelta(days=1)
    return end


def recurring_occurrence(rule, today):
    """오늘이 속한 주기(주/월)의 (주기 키, 발생 날짜). 주: 월~일 기준, 월: 해당 월."""
    if rule["freq"] == "monthly":
        last_day = calendar.monthrange(today.year, today.month)[1]
        if rule["when"] == "last":
            occ = _last_workday(date(today.year, today.month, 1), date(today.year, today.month, last_day))
        else:
            occ = date(today.year, today.month, min(int(rule["when"]), last_day))
        return f"{today.year}-{today.month:02d}", occ
    ws = today - timedelta(days=today.weekday())
    iso = today.isocalendar()
    if rule["when"] == "last":  # 그 주의 마지막 근무일 (금요일이 공휴일이면 목요일)
        occ = _last_workday(ws, ws + timedelta(days=4))
    else:
        occ = ws + timedelta(days=(int(rule["when"]) + 6) % 7)
    return f"{iso[0]}-W{iso[1]:02d}", occ


def ensure_recurring():
    """이번 주기의 정기 업무가 아직 없으면 만든다 (규칙마다 주기당 한 번). 만든 업무 수를 돌려준다."""
    doc = load_recurring()
    if not doc["items"]:
        return 0
    today = date.today()
    archived = archived_projects()
    existing = {(t["recurring"], t["due"]) for t in list_tasks(include_archived=True) if t.get("recurring")}
    made, changed = 0, False
    for r in doc["items"]:
        try:
            key, occ = recurring_occurrence(r, today)
        except Exception:
            continue
        if r.get("last_period") == key or occ.isoformat() < (r.get("since") or ""):
            continue
        if r.get("project") in archived:  # 보관한 프로젝트는 되살리지 않는다
            continue
        if (r["id"], occ.isoformat()) not in existing:
            create_task({"title": f"{r['title']} ({occ:%m-%d})", "project": r.get("project") or "기타",
                         "due": occ.isoformat(), "priority": r.get("priority") or "보통",
                         "checks": r.get("checks") or "", "recurring": r["id"]})
            made += 1
        r["last_period"] = key
        changed = True
    if changed:
        with lock:
            fresh = load_recurring()  # 만드는 동안 규칙이 바뀌었을 수 있어 last_period만 옮긴다
            keys = {r["id"]: r.get("last_period") for r in doc["items"]}
            for r in fresh["items"]:
                if keys.get(r.get("id")):
                    r["last_period"] = keys[r["id"]]
            _save_recurring(fresh)
    return made


# ---------- 주간 노트 · 보고 ----------

WEEKLY_DIR_NAME = "주간"
WEEKLY_BEGIN, WEEKLY_END = "<!-- 업무보드:자동 시작 -->", "<!-- 업무보드:자동 끝 -->"


def week_bounds(week=None):
    """'2026-W42' (없으면 이번 주) → (월요일, 일요일, 주 키)."""
    if week:
        m = re.fullmatch(r"(\d{4})-W(\d{2})", week.strip())
        if not m:
            raise ValueError("주 형식은 2026-W42")
        start = date.fromisocalendar(int(m.group(1)), int(m.group(2)), 1)
    else:
        t = date.today()
        start = t - timedelta(days=t.weekday())
    iso = start.isocalendar()
    return start, start + timedelta(days=6), f"{iso[0]}-W{iso[1]:02d}"


def week_title(start, end):
    th = start + timedelta(days=3)  # 목요일이 속한 달의 몇 번째 주 (ISO 방식)
    return f"{th.month}월 {(th.day - 1) // 7 + 1}주차 ({start:%m/%d} ~ {end:%m/%d})"


def _rule_label(r):
    if r.get("freq") == "monthly":
        return "매월 " + ("마지막 근무일" if r.get("when") == "last" else f"{int(r['when'])}일")
    return "매주 " + ("마지막 근무일" if r.get("when") == "last" else WEEKDAY_KO[int(r["when"])] + "요일")


def build_weekly(week=None):
    start, end, key = week_bounds(week)
    s, e, today = start.isoformat(), end.isoformat(), date.today()
    personal = personal_projects()  # 개인(성장) 프로젝트는 업무 보고에서 뺀다
    ts = [t for t in list_tasks(include_archived=True) if t["project"] not in personal]
    ref = min(today, end)  # 지난 주는 그 주 일요일, 이번 주는 오늘 기준으로 지연을 센다
    done = sorted([t for t in ts if t["status"] == "완료" and s <= (t["completed"] or "") <= e], key=lambda t: t["completed"])
    doing = sorted([t for t in ts if t["status"] in ("진행중", "할일") and not t["archived"]
                    and (t["status"] == "진행중" or (t["due"] and t["due"] <= e))], key=lambda t: t["due"] or "9999")
    waiting = sorted([t for t in ts if t["status"] == "대기" and not t["archived"]], key=wait_since)
    nxt_to = (end + timedelta(days=7)).isoformat()
    nxt = sorted([t for t in ts if t["status"] in ("진행중", "할일") and not t["archived"] and t["due"]
                  and e < t["due"] <= nxt_to], key=lambda t: t["due"])
    issues = list_issues()
    new_issues = [i for i in issues if s <= i["date"] <= e]
    open_issues = [i for i in issues if i["status"] in OPEN_ISSUE_STATUS]
    rules = load_recurring()["items"]

    def short(t):
        return t["title"].replace(t["project"] + " - ", "")

    def md_day(iso):
        return iso[5:].replace("-", "/")

    def when_of(t):
        if not t["due"]:
            return "마감 미정"
        late = (ref - date.fromisoformat(t["due"])).days
        return f"마감 {md_day(t['due'])}" + (f" · 지연 {late}일" if late > 0 else "")

    def waited(t):
        return f"{max(0, (today - date.fromisoformat(wait_since(t))).days)}일째 대기"

    md = [f"## 📊 업무 요약 (자동 생성 · {today:%m/%d} 기준)",
          f"- 완료 {len(done)} · 진행·이월 {len(doing)} · 대기 {len(waiting)} · 다음 주 예정 {len(nxt)} · 미해결 이슈 {len(open_issues)}", ""]

    def block(title, rows, fmt):
        if rows:
            md.append(f"### {title} ({len(rows)})")
            md.extend(fmt(r) for r in rows)
            md.append("")
    block("✅ 이번 주 완료", done, lambda t: f"- [[{t['id'][:-3]}]] · 완료 {md_day(t['completed'])}")
    block("🔧 진행·이월", doing, lambda t: f"- [[{t['id'][:-3]}]] · {t['status']} · {when_of(t)}")
    block("❓ 대기·확인", waiting, lambda t: f"- [[{t['id'][:-3]}]] · {waited(t)}")
    block("📅 다음 주 예정", nxt, lambda t: f"- [[{t['id'][:-3]}]] · 마감 {md_day(t['due'])}")
    block("🐞 이번 주 등록 이슈", new_issues, lambda i: f"- [[{i['stem']}]] · {i['status'] or '-'}")
    block("🚧 미해결 이슈", open_issues, lambda i: f"- [[{i['stem']}]] · {i['status']} · {i['date']}")
    block("🔁 정기 업무", rules, lambda r: f"- {r['title']} · {_rule_label(r)}")
    markdown = "\n".join(md).rstrip() + "\n"

    tx = [f"[주간 업무 보고] {key} · {week_title(start, end)}"]

    def tblock(title, rows, fmt):
        if rows:
            tx.extend(["", f"■ {title} ({len(rows)})"])
            tx.extend(f" - {fmt(r)}" for r in rows)
    tblock("이번 주 완료", done, lambda t: f"[{t['project']}] {short(t)}")
    tblock("진행·이월", doing, lambda t: f"[{t['project']}] {short(t)} ({when_of(t)})")
    tblock("대기·확인", waiting, lambda t: f"[{t['project']}] {short(t)} ({waited(t)})")
    tblock("다음 주 예정", nxt, lambda t: f"[{t['project']}] {short(t)} (마감 {md_day(t['due'])})")
    tblock("미해결 이슈", open_issues, lambda i: f"{i['title']} ({i['status']})")
    if len(tx) == 1:
        tx.append("(이번 주에 정리할 업무가 없어요)")
    note = f"{WEEKLY_DIR_NAME}/{key}.md"
    return {"week": key, "start": s, "end": e, "title": week_title(start, end), "note": note,
            "note_exists": (Path(load_config()["work_dir"]) / note).exists(),
            "counts": {"done": len(done), "doing": len(doing), "waiting": len(waiting), "next": len(nxt), "issues": len(open_issues)},
            "markdown": markdown, "text": "\n".join(tx) + "\n"}


def save_weekly(data):
    """주간 노트를 만들거나 갱신한다. 이미 있는 노트는 자동 구간(<!-- 업무보드 -->)만 바꾸고 손으로 쓴 부분은 건드리지 않는다."""
    r = build_weekly((data or {}).get("week"))
    d = Path(load_config()["work_dir"]) / WEEKLY_DIR_NAME
    p = d / f"{r['week']}.md"
    block = f"{WEEKLY_BEGIN}\n{r['markdown'].rstrip()}\n{WEEKLY_END}"
    with lock:
        d.mkdir(parents=True, exist_ok=True)
        if not p.exists():
            text = (f"---\ntype: 주간\nweek: {r['week']}\nstart: {r['start']}\nend: {r['end']}\ntags: [work, 주간]\n---\n\n"
                    f"# {r['title']}\n\n{block}\n\n## 회고 (주말 전에)\n- \n")
            p.write_text(text, encoding="utf-8", newline="")
            return {"path": r["note"], "created": True}
        raw = p.read_bytes().decode("utf-8")
        nl = "\r\n" if "\r\n" in raw else "\n"
        text = raw.replace("\r\n", "\n")
        i, j = text.find(WEEKLY_BEGIN), text.find(WEEKLY_END)
        if i >= 0 and j > i:
            text = text[:i] + block + text[j + len(WEEKLY_END):]
        else:
            text = text.rstrip("\n") + "\n\n" + block + "\n"
        p.write_bytes(text.replace("\n", nl).encode("utf-8"))
    return {"path": r["note"], "created": False}


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
           f'<actions><action content="콕핏 열기" activationType="protocol" arguments="{escape(url)}"/>'
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


# 화면(업무보드 창) 안에 크게 띄우는 알림 대기열. 열려 있는 업무보드가 주기적으로 가져가서 팝업으로 보여 준다.
ALERTS = []
LAST_POLL = 0.0   # 마지막으로 업무보드 화면이 알림을 가져간 시각
ALERT_SEQ = 0


def raise_alert(kind, title, lines):
    """알림을 화면 팝업 대기열에 넣는다. 업무보드가 열려 있지 않으면(최근 90초 안에 확인한 화면이 없으면) Windows 알림도 보낸다."""
    global ALERT_SEQ
    with lock:
        ALERT_SEQ += 1
        # 같은 종류의 아직 확인 안 한 알림은 새 것으로 교체한다 (쌓이지 않게)
        ALERTS[:] = [a for a in ALERTS if a["kind"] != kind]
        ALERTS.append({"id": ALERT_SEQ, "kind": kind, "title": title, "lines": [l for l in lines if l], "ts": time.time()})
    if time.time() - LAST_POLL > 90:
        try:
            show_toast(title, lines)
        except Exception as e:
            print("Windows 알림 실패:", e)


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


URGENT_MARK = "🔥"  # 할 일 앞에 이 표시가 붙어 있으면 '긴급' (노트에는 `- [ ] 🔥 내용`으로 저장)


def urgent_items(tasks=None):
    """끝나지 않은 업무의 안 한 긴급 할 일 [(업무, 항목 번호, 내용)]. 마감이 가까운 업무가 먼저."""
    out = []
    for t in (list_tasks() if tasks is None else tasks):
        if t["status"] in ("완료", "보류"):
            continue
        for i, c in enumerate(t["checks"]):
            if not c["done"] and c["text"].lstrip().startswith(URGENT_MARK):
                out.append((t, i, c["text"].lstrip()[len(URGENT_MARK):].strip()))
    out.sort(key=lambda x: x[0]["due"] or "9999")
    return out


def urgent_lines(items, n=3):
    names = [f"{t['project']} · {txt}" for t, _, txt in items[:n]]
    return ", ".join(names) + (f" 외 {len(items) - n}건" if len(items) > n else "")


def build_alarm(kind):
    """알림 내용 (title, lines). 보낼 게 없으면 None."""
    if kind == "urgent":
        items = urgent_items()
        if not items:
            return None
        return f"🔥 긴급 {len(items)}건 — 먼저 처리하세요", [urgent_lines(items)]
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
        urg = urgent_items()
        if urg:
            lines.append(f"🔥 긴급 {len(urg)}건: " + urgent_lines(urg, 2))
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
        urg = urgent_items()
        if not due_today and not late and not urg:
            return None
        lines = []
        if urg:
            lines.append(f"🔥 긴급 {len(urg)}건: " + urgent_lines(urg, 2))
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
            if state.get("recurring") != today:  # 서버만 켜 둬도 그날의 정기 업무가 만들어지게 하루 한 번
                state["recurring"] = today
                STATE_PATH.write_text(json.dumps(state), encoding="utf-8")
                try:
                    ensure_recurring()
                except Exception as e:
                    print("정기 업무 생성 실패:", e)
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
                        raise_alert(kind, *msg)
            # 긴급 할 일은 평일 근무 시간에 일정 간격으로 계속 알려 준다 (urgent_every 분, 0이면 끔)
            every = int(load_config().get("urgent_every", 60) or 0)
            if every > 0 and now.weekday() < 5 and not is_holiday(now.date()) and "09:00" <= hm <= "18:00":
                last = float(state.get("urgent_last", 0))
                if time.time() - last >= every * 60:
                    state["urgent_last"] = time.time()
                    STATE_PATH.write_text(json.dumps(state), encoding="utf-8")
                    msg = build_alarm("urgent")
                    if msg:
                        raise_alert("urgent", *msg)
        except Exception as e:
            print("알림 오류:", e)
        time.sleep(30)


# ---------- 성장(회고·목표·선물) ----------

GROWTH_DIR_NAME = "성장"
REVIEW_KINDS = ("주간", "월간", "프로젝트")


def growth_dir():
    return Path(load_config()["work_dir"]) / GROWTH_DIR_NAME


VAULT_SETTINGS_FILE = ".업무보드-설정.json"  # 볼트(Work 폴더) 안에 둬서 웹 버전·다른 PC와 개인 프로젝트 목록을 공유한다


def _vault_settings_path():
    return Path(load_config()["work_dir"]) / VAULT_SETTINGS_FILE


def _load_vault_settings():
    try:
        d = json.loads(_vault_settings_path().read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _save_vault_settings(doc):
    p = _vault_settings_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")


def personal_projects():
    doc = _load_vault_settings()
    if isinstance(doc.get("personal_projects"), list):
        return set(doc["personal_projects"])
    old = load_config().get("personal_projects") or []  # 예전 방식(config.json) → 한 번만 볼트 파일로 옮긴다
    if old:
        try:
            doc["personal_projects"] = list(old)
            _save_vault_settings(doc)
        except Exception:
            pass
    return set(old) if old else {"개인업무"}  # 한 번도 지정한 적 없으면 '개인업무'를 개인 프로젝트로 본다


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

    def _cors_origin(self):
        """차트 체크 API만 군영 대시보드(허용 목록의 출처)에서 부를 수 있다. 그 밖의 API는 열지 않는다."""
        o = self.headers.get("Origin") or ""
        return o if any(o == a or o.startswith(a + ":") for a in CHART_ORIGINS) else ""

    def send_json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        if urlparse(self.path).path.startswith("/api/chart/") and self._cors_origin():
            self.send_header("Access-Control-Allow-Origin", self._cors_origin())
            self.send_header("Vary", "Origin")
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
                try:
                    ensure_recurring()  # 이번 주기의 정기 업무를 아직 안 만들었으면 만든다
                except Exception as e:
                    print("정기 업무 생성 실패:", e)
                self.send_json({"tasks": list_tasks(charts=True), "archived": archived_summary(), "colors": project_colors(),
                                "personal": sorted(personal_projects()), "today": date.today().isoformat()})
            elif u.path == "/api/issues":
                self.send_json({"issues": list_issues()})
            elif u.path == "/api/recurring":
                self.send_json(load_recurring())
            elif u.path == "/api/weekly":
                self.send_json(build_weekly((parse_qs(u.query).get("week") or [None])[0]))
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
            elif u.path == "/api/alerts":
                global LAST_POLL
                LAST_POLL = time.time()
                with lock:
                    self.send_json({"alerts": list(ALERTS)})
            elif u.path == "/api/chart/state":  # 군영 대시보드가 열릴 때 체크·메모 상태를 읽어 간다
                cards = {t["symbol"]: {"checked": t["status"] == "완료", "status": t["status"],
                                       "theme": ", ".join(x for x in t["themes"] if x != "미분류"), "memo": _memo_of(t)}
                         for t in list_tasks(charts=True) if t["board"] == CHART_BOARD and t["symbol"]}
                self.send_json({"cards": cards})
            elif u.path == "/api/config":
                cfg = load_config()
                self.send_json({"ics_count": len(cfg["ics_urls"]), "work_dir": cfg["work_dir"], "alarms": cfg.get("alarms", {}),
                                "urgent_every": cfg.get("urgent_every", 60)})
            else:
                self.send_json({"error": "없는 주소"}, 404)
        except Exception as e:
            self.send_json({"error": str(e)}, 500)

    def do_OPTIONS(self):  # 브라우저의 사전 확인(preflight): 차트 체크 API + 허용된 출처만
        u = urlparse(self.path)
        origin = self._cors_origin()
        if not (u.path.startswith("/api/chart/") and origin):
            self.send_response(403)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", origin)
        self.send_header("Access-Control-Allow-Methods", "POST, GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Private-Network", "true")
        self.send_header("Access-Control-Max-Age", "600")
        self.send_header("Vary", "Origin")
        self.end_headers()

    def do_POST(self):
        u = urlparse(self.path)
        try:
            if u.path.startswith("/api/chart/") and self.headers.get("Origin") and not self._cors_origin():
                self.send_json({"error": "허용되지 않은 출처"}, 403)
                return
            data = self.read_json()
            if u.path == "/api/chart/scout":
                self.send_json({"ok": True, "task": upsert_chart_scout(data)})
            elif u.path == "/api/task/update":
                self.send_json({"task": update_task(data)})
            elif u.path == "/api/recurring/list":
                self.send_json(save_recurring_list(data))
            elif u.path == "/api/weekly/save":
                self.send_json(save_weekly(data))
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
                names = [s.strip() for s in data.get("projects", []) if s.strip()]
                cfg["personal_projects"] = names
                save_config(cfg)
                doc = _load_vault_settings()
                doc["personal_projects"] = names
                _save_vault_settings(doc)
                self.send_json({"personal": names})
            elif u.path == "/api/project/rename":
                self.send_json(rename_project(data.get("from"), data.get("to")))
            elif u.path == "/api/project/delete":
                self.send_json(delete_project(data.get("project")))
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
                if "urgent_every" in data:
                    ue = int(data.get("urgent_every") or 0)
                    if ue < 0 or ue > 1440:
                        raise ValueError("긴급 알림 간격은 0~1440분")
                    cfg["urgent_every"] = ue
                save_config(cfg)
                self.send_json({"alarms": alarms, "urgent_every": cfg.get("urgent_every", 60)})
            elif u.path == "/api/notify/test":
                msg = build_alarm(data.get("kind", "morning")) or ("보낼 알림이 없어요", ["오늘 마감·지연 업무가 없어요."])
                raise_alert(data.get("kind", "morning"), *msg)
                self.send_json({"title": msg[0], "lines": msg[1]})
            elif u.path == "/api/alerts/ack":
                with lock:
                    if data.get("id") is None:
                        ALERTS.clear()
                    else:
                        ALERTS[:] = [a for a in ALERTS if a["id"] != int(data["id"])]
                self.send_json({"ok": True})
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
    print(f"콕핏 실행 중: http://127.0.0.1:{port}  (끄려면 이 창을 닫으세요)")
    if "--no-browser" not in __import__("sys").argv:
        threading.Timer(0.8, lambda: webbrowser.open(f"http://127.0.0.1:{port}")).start()
    srv.serve_forever()


if __name__ == "__main__":
    main()
