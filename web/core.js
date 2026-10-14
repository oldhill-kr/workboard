/* 업무보드 웹 코어: server.py가 하던 일(노트 읽기/쓰기, 보관, 이름 변경, 색 고정 …)을 브라우저에서 한다.
 * 저장소(어댑터)는 파일 몇 개를 읽고 쓰는 단순한 인터페이스만 있으면 된다:
 *   list(dir) → [{name, mtime(초)}], listAll() → [경로], read(path) → 문자열|null,
 *   write(path, text), remove(path), label
 * 경로는 볼트(Work 폴더) 기준 상대 경로: "업무/KC - 제목.md", "_휴지통/…", ".업무보드-색상.json"
 */
(function (root) {
  const STATUSES = ["할일", "진행중", "대기", "완료", "보류"];
  const CHECK_RE = /^(\s*[-*] \[)( |x|X)(\] )(.*)$/;
  const FM_RE = /^---\r?\n([\s\S]*?)\r?\n---\r?\n?/;
  const BAD_NAME_RE = /[\\/:*?"<>|#^\[\]]/;
  const PALETTE = ["#3b6ff5", "#e5484d", "#2f9e5b", "#d98a00", "#8e4ec6", "#0f9fb5", "#d6409f", "#6b7a2e", "#c4622d", "#5b6b8c",
    "#84a800", "#a21caf", "#38a3f5", "#f97362", "#10b981", "#6d28d9", "#8b5e3c", "#be123c", "#c2a000", "#6c8ebf"];
  const TASK_DIR = "업무", TRASH_DIR = "_휴지통", COLORS_FILE = ".업무보드-색상.json";

  let A = null;                 // 저장소 어댑터
  let CAL = null;               // 구글 캘린더 제공자 (없으면 일정 없음)
  const cache = new Map();      // 경로 → {text, mtime}
  let chain = Promise.resolve(); // 요청을 한 줄로 세워 동시 수정 방지
  let settings = { vault: "", alarms: { morning: "08:40", evening: "17:00", friday: "16:00", review_week: "16:30", review_month: "16:30" } };

  const lsGet = (k, d) => { try { const v = localStorage.getItem(k); return v == null ? d : JSON.parse(v); } catch (e) { return d; } };
  const lsSet = (k, v) => { try { localStorage.setItem(k, JSON.stringify(v)); } catch (e) {} };

  const pad = n => String(n).padStart(2, "0");
  const isoDate = d => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
  const todayISO = () => isoDate(new Date());
  const addDaysISO = (iso, n) => { const d = new Date(iso + "T00:00:00"); d.setDate(d.getDate() + n); return isoDate(d); };
  const nowSec = () => Date.now() / 1000;
  const norm = t => t.replace(/\r\n/g, "\n");
  const escRe = s => s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const fail = msg => { throw new Error(msg); };

  // ---------- 노트 읽기/쓰기 ----------
  function parseValue(raw) {
    raw = raw.trim();
    if (raw.startsWith("[") && raw.endsWith("]"))
      return raw.slice(1, -1).split(",").map(v => v.trim().replace(/^['"]|['"]$/g, "")).filter(v => v);
    return raw.replace(/^['"]|['"]$/g, "");
  }
  function readNote(text) {
    text = norm(text);
    const m = FM_RE.exec(text), fm = {};
    let body = text;
    if (m) {
      for (const line of m[1].split("\n")) {
        const i = line.indexOf(":");
        if (i >= 0 && !/^\s/.test(line)) fm[line.slice(0, i).trim()] = parseValue(line.slice(i + 1));
      }
      body = text.slice(m[0].length);
    }
    return { fm, body, text };
  }
  function setFrontmatter(text, key, value) {
    const m = FM_RE.exec(text);
    const line = `${key}: ${value}`.trimEnd();
    if (!m) return `---\n${line}\n---\n` + text;
    const lines = m[1].split("\n"), pat = new RegExp("^" + escRe(key) + "\\s*:");
    const i = lines.findIndex(l => pat.test(l));
    if (i >= 0) lines[i] = line; else lines.push(line);
    return "---\n" + lines.join("\n") + "\n---\n" + text.slice(m[0].length);
  }
  function removeFrontmatter(text, key) {
    const m = FM_RE.exec(text);
    if (!m) return text;
    const pat = new RegExp("^" + escRe(key) + "\\s*:");
    return "---\n" + m[1].split("\n").filter(l => !pat.test(l)).join("\n") + "\n---\n" + text.slice(m[0].length);
  }
  const NOTE_BULLET_RE = /^(\s*[-*] )(?!\[[ xX]\] )(.*)$/;
  function appendLog(text, entry, section = "진행 기록", dated = true) {
    const line = dated ? `- ${todayISO()}: ${entry}` : `- ${entry}`, lines = text.split("\n");
    const start = lines.findIndex(l => l.trim() === `## ${section}`);
    if (start < 0) return text.trimEnd() + `\n\n## ${section}\n` + line + "\n";
    let end = lines.length;
    for (let i = start + 1; i < lines.length; i++) if (lines[i].startsWith("## ")) { end = i; break; }
    let at = end;
    while (at - 1 > start && !lines[at - 1].trim()) at--;
    lines.splice(at, 0, line);
    return lines.join("\n");
  }
  function editNoteLine(text, section, idx, newText) {
    const lines = text.split("\n");
    const start = lines.findIndex(l => l.trim() === `## ${section}`);
    if (start < 0) fail("그 항목을 찾지 못했어요. 새로고침 후 다시 해 주세요");
    let n = -1;
    for (let i = start + 1; i < lines.length; i++) {
      if (lines[i].startsWith("## ")) break;
      const m = NOTE_BULLET_RE.exec(lines[i]);
      if (!m) continue;
      n++;
      if (n === idx) {
        if (newText === null) lines.splice(i, 1); else lines[i] = m[1] + newText;
        return lines.join("\n");
      }
    }
    fail("그 항목을 찾지 못했어요. 새로고침 후 다시 해 주세요");
  }
  function editCheckLine(text, idx, newText) {
    let n = -1, found = false;
    const out = [];
    for (let line of text.split("\n")) {
      const cm = CHECK_RE.exec(line);
      if (cm) {
        n++;
        if (n === idx) {
          found = true;
          if (newText === null) continue;
          line = cm[1] + cm[2] + cm[3] + newText;
        }
      }
      out.push(line);
    }
    if (!found) fail("할 일 항목을 찾지 못했어요. 새로고침 후 다시 해 주세요");
    return out.join("\n");
  }
  function addCheckLine(text, item) {
    const lines = text.split("\n"), nw = `- [ ] ${item}`;
    for (let i = 0; i < lines.length; i++) {
      const cm = CHECK_RE.exec(lines[i]);
      if (cm && !cm[4].trim()) { lines[i] = nw; return lines.join("\n"); }
    }
    const start = lines.findIndex(l => l.trim() === "## 할 일");
    if (start < 0) {
      let at = lines.findIndex(l => l.trim() === "## 진행 기록");
      if (at < 0) at = lines.length;
      lines.splice(at, 0, "## 할 일", nw, "");
      return lines.join("\n");
    }
    let end = lines.length;
    for (let i = start + 1; i < lines.length; i++) if (lines[i].startsWith("## ")) { end = i; break; }
    let last = start;
    for (let i = start + 1; i < end; i++) if (CHECK_RE.test(lines[i])) last = i;
    lines.splice(last + 1, 0, nw);
    return lines.join("\n");
  }

  function taskFromNote(name, text, mtime) {
    const { fm, body } = readNote(text);
    if (fm.type !== "업무") return null;
    const checks = [];
    for (const line of body.split("\n")) {
      const cm = CHECK_RE.exec(line);
      if (cm) checks.push({ text: cm[4], done: cm[2] !== " " });
    }
    const stem = name.replace(/\.md$/, "");
    let title = stem;
    for (const line of body.split("\n")) if (line.startsWith("# ")) { title = line.slice(2).trim(); break; }
    const people = fm.people || [];
    let obsidian = null;
    if (A && A.obsidian && settings.vault)
      obsidian = `obsidian://open?vault=${encodeURIComponent(settings.vault)}&file=${encodeURIComponent((A.folderName || "Work") + "/" + TASK_DIR + "/" + stem)}`;
    return {
      id: name, title, project: fm.project || "기타", status: fm.status || "할일", priority: fm.priority || "보통",
      due: fm.due || null, created: fm.created || null, completed: fm.completed || null,
      archived: String(fm.archived || "").toLowerCase() === "true",
      people: Array.isArray(people) ? people : [people], checks, body, mtime, obsidian,
    };
  }

  // ---------- 저장소 캐시 ----------
  async function syncNotes() {
    const files = (await A.list(TASK_DIR)).filter(f => f.name.endsWith(".md"));
    const seen = new Set();
    for (const f of files) {
      const path = `${TASK_DIR}/${f.name}`;
      seen.add(path);
      const c = cache.get(path);
      if (c && Math.abs(c.mtime - f.mtime) < 0.5) continue;
      const text = await A.read(path);
      if (text != null) cache.set(path, { text, mtime: f.mtime });
    }
    for (const k of [...cache.keys()]) if (k.startsWith(TASK_DIR + "/") && !seen.has(k)) cache.delete(k);
  }
  async function listTasks(includeArchived = false) {
    await syncNotes();
    const out = [];
    for (const [path, c] of [...cache.entries()].sort((a, b) => a[0].localeCompare(b[0]))) {
      if (!path.startsWith(TASK_DIR + "/")) continue;
      try {
        const t = taskFromNote(path.slice(TASK_DIR.length + 1), c.text, c.mtime);
        if (t && (includeArchived || !t.archived)) out.push(t);
      } catch (e) { console.warn("노트 읽기 실패", path, e); }
    }
    return out;
  }
  async function putNote(path, text) {
    await A.write(path, text);
    cache.set(path, { text: norm(text), mtime: nowSec() });
  }
  async function dropNote(path) { await A.remove(path); cache.delete(path); }
  const taskPath = id => {
    if (!id || id.includes("/") || !id.endsWith(".md")) fail("잘못된 업무 id");
    const p = `${TASK_DIR}/${id}`;
    if (!cache.has(p)) fail("잘못된 업무 id");
    return p;
  };

  // ---------- 업무 수정 ----------
  const oneline = s => String(s || "").trim().replace(/[ \t]*\r?\n[ \t]*/g, "<br>");
  async function renameTask(id, title) {
    title = (title || "").trim();
    if (!title) fail("업무 이름이 필요해요");
    if (BAD_NAME_RE.test(title)) fail('이름에 \\ / : * ? " < > | # ^ [ ] 는 쓸 수 없어요');
    const path = taskPath(id), c = cache.get(path);
    const project = readNote(c.text).fm.project || "기타";
    const stem = path.slice(TASK_DIR.length + 1).replace(/\.md$/, "");
    let newStem = `${project} - ${title}`;
    if (newStem === stem) return id;
    let dest = `${TASK_DIR}/${newStem}.md`, i = 2;
    while (dest !== path && cache.has(dest)) dest = `${TASK_DIR}/${newStem} (${i++}).md`;
    newStem = dest.slice(TASK_DIR.length + 1).replace(/\.md$/, "");
    let text = norm(c.text).replace(new RegExp("^# " + escRe(stem) + "\s*$", "m"), () => `# ${newStem}`);
    await putNote(dest, text);
    await dropNote(path);
    await fixLinks({ [stem]: newStem });
    return dest.slice(TASK_DIR.length + 1);
  }
  async function updateTask(data) {
    await syncNotes();
    if (data.new_title != null) data = { ...data, id: await renameTask(data.id, data.new_title) };
    if (data.project != null) data = { ...data, id: await changeTaskProject(data.id, data.project) };
    const path = taskPath(data.id);
    let text = norm(cache.get(path).text);
    if ("status" in data) {
      if (!STATUSES.includes(data.status)) fail("잘못된 상태");
      const old = readNote(text).fm.status;
      text = setFrontmatter(text, "status", data.status);
      text = data.status === "완료" ? setFrontmatter(text, "completed", todayISO()) : removeFrontmatter(text, "completed");
      if (old !== data.status) text = appendLog(text, `상태 ${old} → ${data.status}`);
    }
    if ("due" in data) {
      const due = data.due || "";
      if (due && !/^\d{4}-\d{2}-\d{2}$/.test(due)) fail("마감일 형식은 YYYY-MM-DD");
      text = setFrontmatter(text, "due", due);
    }
    if ("priority" in data) text = setFrontmatter(text, "priority", data.priority);
    if ("toggle_check" in data) {
      const idx = Number(data.toggle_check);
      let n = -1;
      text = text.split("\n").map(line => {
        const cm = CHECK_RE.exec(line);
        if (!cm) return line;
        n++;
        return n === idx ? cm[1] + (cm[2] !== " " ? " " : "x") + cm[3] + cm[4] : line;
      }).join("\n");
    }
    if ("delete_check" in data) text = editCheckLine(text, Number(data.delete_check), null);
    if ("edit_check" in data) {
      const nt = (data.text || "").trim();
      if (!nt) fail("내용을 입력해 주세요");
      text = editCheckLine(text, Number(data.edit_check), oneline(nt));
    }
    if (data.add_check) text = addCheckLine(text, oneline(data.add_check));
    if (data.log) text = appendLog(text, oneline(data.log));
    if (data.add_note) {
      const sec = (data.add_note.section || "진행 기록").trim(), txt = (data.add_note.text || "").trim();
      if (!txt) fail("내용을 입력해 주세요");
      text = appendLog(text, oneline(txt), sec, sec === "진행 기록");
    }
    if (data.edit_note) {
      const txt = (data.edit_note.text || "").trim();
      if (!txt) fail("내용을 입력해 주세요");
      text = editNoteLine(text, (data.edit_note.section || "").trim(), Number(data.edit_note.idx), oneline(txt));
    }
    if (data.delete_note) text = editNoteLine(text, (data.delete_note.section || "").trim(), Number(data.delete_note.idx), null);
    await putNote(path, text);
    return { task: taskFromNote(data.id, text, cache.get(path).mtime) };
  }

  async function createTask(data) {
    const title = (data.title || "").trim();
    if (!title) fail("업무 이름이 필요해요");
    const project = (data.project || "기타").trim();
    await syncNotes();
    if ((await archivedProjects()).has(project)) await setArchived(project, false); // 보관된 프로젝트에 추가하면 다시 꺼낸다
    const name = `${project} - ${title}`.replace(/[\\/:*?"<>|#^\[\]]/g, " ").trim();
    let fname = `${name}.md`, i = 2;
    while (cache.has(`${TASK_DIR}/${fname}`)) fname = `${name} (${i++}).md`;
    const today = todayISO(), stem = fname.replace(/\.md$/, "");
    const checks = (data.checks || "").split("\n").map(c => c.trim()).filter(c => c);
    const checkLines = checks.map(c => `- [ ] ${c}`).join("\n") || "- [ ] ";
    const text = `---
type: 업무
project: ${project}
status: ${data.status || "할일"}
priority: ${data.priority || "보통"}
due: ${data.due || ""}
created: ${today}
people: []
tags: [work, 업무]
---

# ${stem}

## 할 일
${checkLines}

## 진행 기록
- ${today}: 등록 (업무보드)
`;
    await putNote(`${TASK_DIR}/${fname}`, text);
    return { task: taskFromNote(fname, text, cache.get(`${TASK_DIR}/${fname}`).mtime) };
  }

  async function deleteTask(data) {
    await syncNotes();
    const path = taskPath(data.id);
    const text = cache.get(path).text;
    const stem = data.id.replace(/\.md$/, "");
    let dest = `${TRASH_DIR}/${data.id}`;
    if (await A.read(dest) != null) {
      const d = new Date();
      dest = `${TRASH_DIR}/${stem} (${d.getFullYear()}${pad(d.getMonth() + 1)}${pad(d.getDate())}-${pad(d.getHours())}${pad(d.getMinutes())}${pad(d.getSeconds())}).md`;
    }
    await A.write(dest, text);
    await dropNote(path);
    return { id: data.id, trash: dest };
  }

  // ---------- 프로젝트: 보관 / 이름 변경 / 색 ----------
  async function archivedProjects() {
    const state = {};
    for (const t of await listTasks(true)) state[t.project] = (state[t.project] ?? true) && t.archived;
    return new Set(Object.keys(state).filter(p => state[p]));
  }
  async function archivedSummary() {
    const names = await archivedProjects(), out = {};
    for (const t of await listTasks(true)) {
      if (!names.has(t.project)) continue;
      const o = out[t.project] || (out[t.project] = { project: t.project, total: 0, done: 0, mtime: 0 });
      o.total++; o.done += t.status === "완료" ? 1 : 0; o.mtime = Math.max(o.mtime, t.mtime);
    }
    return Object.values(out).sort((a, b) => b.mtime - a.mtime)
      .map(o => ({ ...o, last: o.mtime ? isoDate(new Date(o.mtime * 1000)) : null }));
  }
  async function deleteProject(project) {
    project = (project || "").trim();
    if (!project) fail("프로젝트 이름이 필요해요");
    await syncNotes();
    const ids = [];
    for (const [path, c] of [...cache.entries()]) {
      if (!path.startsWith(TASK_DIR + "/")) continue;
      const { fm } = readNote(c.text);
      if (fm.type === "업무" && (fm.project || "기타") === project) {
        if (String(fm.archived).toLowerCase() !== "true") fail("보관함에 있는 프로젝트만 삭제할 수 있어요");
        ids.push(path.slice(TASK_DIR.length + 1));
      }
    }
    for (const id of ids) await deleteTask({ id });
    return { count: ids.length };
  }
  async function setArchived(project, flag) {
    project = (project || "").trim();
    if (!project) fail("프로젝트 이름이 필요해요");
    await syncNotes();
    let n = 0;
    for (const [path, c] of [...cache.entries()]) {
      if (!path.startsWith(TASK_DIR + "/")) continue;
      const { fm, text } = readNote(c.text);
      if (fm.type !== "업무" || (fm.project || "기타") !== project) continue;
      const nt = flag ? setFrontmatter(text, "archived", "true") : removeFrontmatter(text, "archived");
      if (nt !== text) await putNote(path, nt);
      n++;
    }
    if (!n) fail("그 프로젝트의 업무 노트를 찾지 못했어요");
  }

  async function loadColors() {
    const t = await A.read(COLORS_FILE);
    if (t == null) return null;
    try { return JSON.parse(t); } catch (e) { return null; }
  }
  async function projectColors() {
    const names = new Set((await listTasks(true)).map(t => t.project));
    const saved = (await loadColors()) || {};
    const colors = {};
    for (const [k, v] of Object.entries(saved)) if (names.has(k)) colors[k] = v;
    for (const n of [...names].filter(n => !(n in colors)).sort()) {
      const used = new Set(Object.values(colors));
      const free = PALETTE.filter(c => !used.has(c));
      colors[n] = free.length ? free[0] : PALETTE[Object.keys(colors).length % PALETTE.length];
    }
    if (JSON.stringify(colors) !== JSON.stringify(saved)) await A.write(COLORS_FILE, JSON.stringify(colors, null, 2));
    return colors;
  }
  async function carryColor(oldName, newName) {
    const colors = (await loadColors()) || {};
    if (oldName in colors && !(newName in colors) && !(await listTasks(true)).some(t => t.project === oldName)) {
      colors[newName] = colors[oldName]; delete colors[oldName];
      await A.write(COLORS_FILE, JSON.stringify(colors, null, 2));
    }
  }

  function checkProjectName(nm) {
    if (!nm) fail("프로젝트 이름이 필요해요");
    if (BAD_NAME_RE.test(nm)) fail('이름에 \\ / : * ? " < > | # ^ [ ] 는 쓸 수 없어요');
  }
  async function retargetNote(path, text, oldP, newP) {
    const stem = path.slice(TASK_DIR.length + 1).replace(/\.md$/, ""), prefix = `${oldP} - `;
    const newStem = stem.startsWith(prefix) ? `${newP} - ${stem.slice(prefix.length)}` : stem;
    text = setFrontmatter(norm(text), "project", newP);
    if (newStem !== stem) text = text.replace(new RegExp("^# " + escRe(stem) + "\\s*$", "m"), `# ${newStem}`);
    let dest = `${TASK_DIR}/${newStem}.md`, i = 2;
    while (dest !== path && cache.has(dest)) dest = `${TASK_DIR}/${newStem} (${i++}).md`;
    await putNote(dest, text);
    if (dest !== path) await dropNote(path);
    return { dest: dest.slice(TASK_DIR.length + 1), stem };
  }
  async function fixLinks(moved) {
    let n = 0;
    for (const p of await A.listAll()) {
      if (p.startsWith(TRASH_DIR + "/")) continue;
      let t = await A.read(p);
      if (t == null) continue;
      let t2 = t;
      for (const [o, nn] of Object.entries(moved)) t2 = t2.replace(new RegExp("\\[\\[" + escRe(o) + "(?=[\\]|#])", "g"), "[[" + nn);
      if (t2 !== t) {
        await A.write(p, t2);
        if (cache.has(p)) cache.set(p, { text: norm(t2), mtime: nowSec() });
        n++;
      }
    }
    return n;
  }
  async function renameProject(data) {
    const oldP = (data.from || "").trim(), newP = (data.to || "").trim();
    checkProjectName(newP);
    if (!oldP) fail("프로젝트 이름이 필요해요");
    if (oldP === newP) fail("이름이 같아요");
    await syncNotes();
    const notes = [];
    for (const [path, c] of [...cache.entries()]) {
      if (!path.startsWith(TASK_DIR + "/")) continue;
      const { fm } = readNote(c.text);
      if (fm.type === "업무" && (fm.project || "기타") === oldP) notes.push([path, c.text]);
    }
    if (!notes.length) fail("그 프로젝트의 업무 노트를 찾지 못했어요");
    const moved = {};
    for (const [path, text] of notes) {
      const r = await retargetNote(path, text, oldP, newP);
      if (r.dest.replace(/\.md$/, "") !== r.stem) moved[r.stem] = r.dest.replace(/\.md$/, "");
    }
    const links = Object.keys(moved).length ? await fixLinks(moved) : 0;
    await carryColor(oldP, newP);
    return { count: notes.length, renamed: Object.keys(moved).length, links_updated: links };
  }
  async function changeTaskProject(id, newP) {
    newP = (newP || "").trim();
    checkProjectName(newP);
    const path = taskPath(id), c = cache.get(path), oldP = readNote(c.text).fm.project || "기타";
    if (oldP === newP) return id;
    const r = await retargetNote(path, c.text, oldP, newP);
    if (r.dest.replace(/\.md$/, "") !== r.stem) await fixLinks({ [r.stem]: r.dest.replace(/\.md$/, "") });
    await carryColor(oldP, newP);
    return r.dest;
  }

  // ---------- 공휴일 · 일정 · 설정 ----------
  function listHolidays(start, end) {
    const all = root.WB_HOLIDAYS || {}, out = {};
    for (const [d, v] of Object.entries(all)) if (d >= start && d < end) out[d] = v;
    return out;
  }


  // ---------- 성장: 회고 · 목표(선물) · 루틴 · 개인 프로젝트 (서버 버전과 같은 파일 형식) ----------
  const GROWTH_DIR = "성장", ROUTINES_PATH = "성장/루틴.json", SETTINGS_PATH = ".업무보드-설정.json";
  const REVIEW_KINDS = ["주간", "월간", "프로젝트"];
  const cleanName = s => s.replace(/[\\/:*?"<>|#^\[\]]/g, " ").trim();
  const safeText = s => (s || "").trim().replace(/^#/gm, "＃");
  function sectionsOf(body) {
    const out = {}; let cur = null;
    for (const line of norm(body).split("\n")) {
      if (line.startsWith("## ")) { cur = line.slice(3).trim(); out[cur] = []; }
      else if (cur !== null) out[cur].push(line);
    }
    for (const k of Object.keys(out)) out[k] = out[k].join("\n").trim();
    return out;
  }
  async function readJson(path) {
    const t = await A.read(path);
    if (t == null) return null;
    try { return JSON.parse(t); } catch (e) { return null; }
  }
  async function personalList() {
    const d = await readJson(SETTINGS_PATH);
    // 한 번도 지정한 적 없으면 '개인업무' 프로젝트를 개인(성장 보드) 쪽으로 본다
    return d && Array.isArray(d.personal_projects) ? d.personal_projects : ["개인업무"];
  }
  async function listGrowth() {
    const reviews = [], goals = [];
    for (const f of (await A.list(GROWTH_DIR)).filter(x => x.name.endsWith(".md"))) {
      const text = await A.read(`${GROWTH_DIR}/${f.name}`);
      if (text == null) continue;
      const { fm, body } = readNote(text);
      if (fm.type === "회고") {
        const sec = sectionsOf(body);
        reviews.push({ id: f.name, kind: fm.kind || "주간", period: fm.period || "", project: fm.project || "", rating: parseInt(fm.rating || "0", 10) || 0,
          created: fm.created || "", good: sec["잘한 점"] || "", bad: sec["아쉬운 점·부족한 부분"] || "", next: sec["다음 다짐"] || "", mtime: f.mtime });
      } else if (fm.type === "목표") {
        let title = f.name.replace(/\.md$/, "");
        for (const l of norm(body).split("\n")) if (l.startsWith("# ")) { title = l.slice(2).trim(); break; }
        goals.push({ id: f.name, title, status: fm.status || "진행중", project: fm.project || "", reward: fm.reward || "", due: fm.due || null,
          created: fm.created || "", achieved: fm.achieved || null, reward_claimed: String(fm.reward_claimed).toLowerCase() === "true", mtime: f.mtime });
      }
    }
    reviews.sort((a, b) => (b.created.localeCompare(a.created)) || (b.mtime - a.mtime));
    goals.sort((a, b) => ((a.status === "달성") - (b.status === "달성")) || (b.mtime - a.mtime));
    return { reviews, goals, personal: (await personalList()).sort(), today: todayISO() };
  }
  async function saveReview(d) {
    const kind = (d.kind || "주간").trim();
    if (!REVIEW_KINDS.includes(kind)) fail("회고 종류가 올바르지 않아요");
    const period = (d.period || "").trim();
    if (!period) fail("기간을 입력해 주세요");
    const project = (d.project || "").trim();
    const rating = Math.max(0, Math.min(5, parseInt(d.rating || "0", 10) || 0));
    const good = safeText(d.good), bad = safeText(d.bad), next = safeText(d.next);
    if (!good && !bad && !next) fail("잘한 점, 아쉬운 점, 다음 다짐 중 하나는 적어 주세요");
    const name = cleanName(`회고 - ${period}` + (project ? ` - ${project}` : ""));
    const path = `${GROWTH_DIR}/${name}.md`;
    let created = todayISO();
    const old = await A.read(path);
    if (old != null) created = readNote(old).fm.created || created;   // 같은 기간·프로젝트는 덮어써서 고친다
    const text = `---
type: 회고
kind: ${kind}
period: ${period}
project: ${project}
rating: ${rating}
created: ${created}
tags: [work, 회고]
---

# ${name}

## 잘한 점
${good}

## 아쉬운 점·부족한 부분
${bad}

## 다음 다짐
${next}
`;
    await A.write(path, text);
    return { review: (await listGrowth()).reviews.find(r => r.id === `${name}.md`) };
  }
  async function createGoal(d) {
    const title = (d.title || "").trim();
    if (!title) fail("목표를 입력해 주세요");
    const base = cleanName(`목표 - ${title}`);
    let name = `${base}.md`, i = 2;
    while ((await A.read(`${GROWTH_DIR}/${name}`)) != null) name = `${base} (${i++}).md`;
    const text = `---
type: 목표
status: 진행중
project: ${(d.project || "").trim()}
reward: ${(d.reward || "").trim()}
due: ${(d.due || "").trim()}
created: ${todayISO()}
achieved:
reward_claimed: false
tags: [work, 목표]
---

# ${title}
`;
    await A.write(`${GROWTH_DIR}/${name}`, text);
    return { goal: (await listGrowth()).goals.find(g => g.id === name) };
  }
  async function updateGoal(d) {
    const path = `${GROWTH_DIR}/${d.id}`;
    if (!d.id || d.id.includes("/")) fail("잘못된 id");
    let text = await A.read(path);
    if (text == null) fail("잘못된 id");
    text = norm(text);
    if ("status" in d) {
      if (!["진행중", "달성"].includes(d.status)) fail("잘못된 상태");
      text = setFrontmatter(text, "status", d.status);
      text = setFrontmatter(text, "achieved", d.status === "달성" ? todayISO() : "");
      if (d.status !== "달성") text = setFrontmatter(text, "reward_claimed", "false");
    }
    if ("reward_claimed" in d) text = setFrontmatter(text, "reward_claimed", d.reward_claimed ? "true" : "false");
    if ("reward" in d) text = setFrontmatter(text, "reward", (d.reward || "").trim());
    if ("due" in d) text = setFrontmatter(text, "due", (d.due || "").trim());
    await A.write(path, text);
    return { goal: (await listGrowth()).goals.find(g => g.id === d.id) };
  }
  async function deleteGrowth(d) {
    if (!d.id || d.id.includes("/")) fail("잘못된 id");
    const path = `${GROWTH_DIR}/${d.id}`, text = await A.read(path);
    if (text == null) fail("잘못된 id");
    let dest = `${TRASH_DIR}/${d.id}`;
    if ((await A.read(dest)) != null) {
      const n = new Date();
      dest = `${TRASH_DIR}/${d.id.replace(/\.md$/, "")} (${n.getFullYear()}${pad(n.getMonth() + 1)}${pad(n.getDate())}-${pad(n.getHours())}${pad(n.getMinutes())}${pad(n.getSeconds())}).md`;
    }
    await A.write(dest, text);
    await A.remove(path);
    return { id: d.id };
  }
  async function loadRoutines() {
    const d = (await readJson(ROUTINES_PATH)) || {};
    return { routines: Array.isArray(d.routines) ? d.routines : [], checks: d.checks && typeof d.checks === "object" ? d.checks : {} };
  }
  const saveRoutines = doc => A.write(ROUTINES_PATH, JSON.stringify(doc, null, 1));
  async function setRoutineCheck(d) {
    const rid = String(d.routine || ""), date = String(d.date || "");
    if (!/^\d{4}-\d{2}-\d{2}$/.test(date)) fail("날짜 형식은 YYYY-MM-DD");
    const doc = await loadRoutines();
    if (!doc.routines.some(r => r.id === rid)) fail("없는 루틴이에요. 새로고침 후 다시 해 주세요");
    const month = (doc.checks[date.slice(0, 7)] = doc.checks[date.slice(0, 7)] || {});
    const days = new Set(month[rid] || []), day = Number(date.slice(8));
    if (d.done) days.add(day); else days.delete(day);
    month[rid] = [...days].sort((a, b) => a - b);
    await saveRoutines(doc);
    return { ok: true };
  }
  async function saveRoutineList(d) {
    const out = [], seen = new Set();
    for (const it of d.routines || []) {
      const name = (it.name || "").trim();
      if (!name) continue;
      let id = String(it.id || "");
      if (!id || seen.has(id)) id = "r" + Math.random().toString(16).slice(2, 10);
      seen.add(id);
      out.push({ id, name: name.slice(0, 40), emoji: (it.emoji || "").trim().slice(0, 4) });
    }
    const doc = await loadRoutines();
    doc.routines = out;
    await saveRoutines(doc);
    return { routines: out };
  }
  async function setPersonal(d) {
    const list = (d.projects || []).map(s => String(s).trim()).filter(s => s);
    const doc = (await readJson(SETTINGS_PATH)) || {};
    doc.personal_projects = list;
    await A.write(SETTINGS_PATH, JSON.stringify(doc, null, 1));
    return { personal: list };
  }
  const isoWeekKeyOf = d => {
    const t = new Date(Date.UTC(d.getFullYear(), d.getMonth(), d.getDate()));
    const day = t.getUTCDay() || 7; t.setUTCDate(t.getUTCDate() + 4 - day);
    const y0 = new Date(Date.UTC(t.getUTCFullYear(), 0, 1));
    return `${t.getUTCFullYear()}-W${pad(Math.ceil(((t - y0) / 86400000 + 1) / 7))}`;
  };

  // ---------- 알림 (브라우저 탭이 열려 있을 때만) ----------
  async function alarmMessage(kind) {
    const today = todayISO(), tomorrow = addDaysISO(today, 1);
    const open = (await listTasks()).filter(t => t.status !== "완료" && t.status !== "보류");
    const late = open.filter(t => t.due && t.due < today).sort((a, b) => a.due.localeCompare(b.due));
    const dueToday = open.filter(t => t.due === today), dueTom = open.filter(t => t.due === tomorrow);
    const short = t => t.title.replace(t.project + " - ", "");
    if (kind === "morning") {
      const names = [...new Set([...dueToday, ...late].map(short))];
      return [`좋은 아침이에요 ☀ 지연 ${late.length} · 오늘 마감 ${dueToday.length} · 내일 마감 ${dueTom.length}`,
        [names.length ? "✅ " + names.slice(0, 3).join(", ") + (names.length > 3 ? ` 외 ${names.length - 3}건` : "") : "오늘 마감인 업무가 없어요."]];
    }
    if (kind === "evening") {
      if (!dueToday.length && !late.length) return null;
      return [`퇴근 전 체크 — 못 끝낸 업무 ${dueToday.length + late.length}건`,
        [(dueToday.length ? "오늘 마감: " + dueToday.slice(0, 3).map(short).join(", ") : ""), (late.length ? `지연 ${late.length}건: ` + late.slice(0, 2).map(short).join(", ") : "")].filter(x => x)];
    }
    if (kind === "friday") return ["금요일 정기 업무 🔁", ["소스 정리 후 NAS 업데이트, 주간 회고 작성"]];
    if (kind === "review_week" || kind === "review_month") {
      const wk = kind === "review_week", per = wk ? isoWeekKeyOf(new Date()) : today.slice(0, 7);
      const g = await listGrowth();
      if (g.reviews.some(r => r.kind === (wk ? "주간" : "월간") && r.period === per)) return null;  // 이미 썼으면 조용히
      const lines = [`${wk ? "이번 주를" : "이번 달을"} 돌아볼 시간이에요. 잘한 점 · 아쉬운 점 · 다음 다짐을 1분만 적어 보세요 (🌱 성장 탭)`];
      if (late.length) lines.push(`참고: 지연된 업무 ${late.length}건`);
      return [`📝 ${wk ? "이번 주" : "이번 달"} 회고 시간`, lines];
    }
    return null;
  }
  function notify(title, lines) {
    if (typeof Notification === "undefined") return false;
    if (Notification.permission !== "granted") { try { Notification.requestPermission(); } catch (e) {} return false; }
    new Notification(title, { body: lines.join("\n") });
    return true;
  }
  const ALARM_UNTIL = { morning: "13:00", evening: "21:00", friday: "21:00", review_week: "21:00", review_month: "21:00" };
  let alarmTimer = null;
  function startAlarms() {
    if (alarmTimer || typeof document === "undefined") return;
    alarmTimer = setInterval(async () => {
      try {
        const now = new Date(), hm = `${pad(now.getHours())}:${pad(now.getMinutes())}`, today = todayISO();
        const state = lsGet("wb.alarmState", {});
        for (const [kind, at] of Object.entries(settings.alarms || {})) {
          if (!at || !(kind in ALARM_UNTIL) || state[kind] === today) continue;
          const H = root.WB_HOLIDAYS || {}, isHol = d => H[isoDate(d)]?.kind === "holiday";
          const wd = now.getDay(), weekend = wd === 0 || wd === 6;
          if (kind === "friday" || kind === "review_week") {
            // 이번 주 마지막 근무일 (금요일이 공휴일이면 목요일)
            if (weekend || isHol(now)) continue;
            let last = true;
            for (let i = 1; i <= 5 - wd; i++) { const d = new Date(now); d.setDate(d.getDate() + i); if (!isHol(d)) { last = false; break; } }
            if (!last) continue;
          } else if (kind === "review_month") {
            // 이번 달 마지막 근무일
            if (weekend || isHol(now)) continue;
            let last = true;
            for (let d = new Date(now.getFullYear(), now.getMonth(), now.getDate() + 1); d.getMonth() === now.getMonth(); d.setDate(d.getDate() + 1)) {
              const w = d.getDay();
              if (w !== 0 && w !== 6 && !isHol(d)) { last = false; break; }
            }
            if (!last) continue;
          } else if (weekend || isHol(now)) continue;
          if (at <= hm && hm <= ALARM_UNTIL[kind]) {
            state[kind] = today; lsSet("wb.alarmState", state);
            const msg = await alarmMessage(kind);
            if (msg) notify(...msg);
          }
        }
      } catch (e) { console.warn("알림 오류", e); }
    }, 30000);
  }

  // ---------- 라우터 ----------
  async function route(path, body) {
    const u = new URL(path, "http://x"), p = u.pathname, q = u.searchParams;
    if (!body) {
      if (p === "/api/tasks") return { tasks: await listTasks(), archived: await archivedSummary(), colors: await projectColors(), today: todayISO() };
      if (p === "/api/events") {
        if (!CAL) return { events: [], errors: [] };
        try { return await CAL.fetch(q.get("start") || todayISO(), q.get("end") || addDaysISO(todayISO(), 42)); }
        catch (e) { return { events: [], errors: [e.message] }; }
      }
      if (p === "/api/holidays") return listHolidays(q.get("start") || "0000-01-01", q.get("end") || "9999-12-31");
      if (p === "/api/growth") return listGrowth();
      if (p === "/api/routines") return loadRoutines();
      if (p === "/api/config") return { ics_count: CAL ? 1 : 0, work_dir: A ? A.label : "", alarms: settings.alarms, static: true };
      fail("없는 주소");
    }
    if (p === "/api/growth/review") return saveReview(body);
    if (p === "/api/growth/goal") return createGoal(body);
    if (p === "/api/growth/goal/update") return updateGoal(body);
    if (p === "/api/growth/delete") return deleteGrowth(body);
    if (p === "/api/routines/check") return setRoutineCheck(body);
    if (p === "/api/routines/list") return saveRoutineList(body);
    if (p === "/api/config/personal") return setPersonal(body);
    if (p === "/api/task/update") return updateTask(body);
    if (p === "/api/task/create") return createTask(body);
    if (p === "/api/task/delete") return deleteTask(body);
    if (p === "/api/project/delete") return deleteProject(body.project);
    if (p === "/api/project/archive") { await setArchived(body.project, body.archived !== false); return { ok: true }; }
    if (p === "/api/project/rename") return renameProject(body);
    if (p === "/api/config/alarms") {
      const a = {};
      for (const k of Object.keys(ALARM_UNTIL)) {
        const v = (body[k] || "").trim();
        if (v && !/^([01]\d|2[0-3]):[0-5]\d$/.test(v)) fail("시각은 HH:MM 형식");
        a[k] = v;
      }
      settings.alarms = a; lsSet("wb.settings", settings);
      if (Object.values(a).some(v => v) && typeof Notification !== "undefined" && Notification.permission === "default") { try { Notification.requestPermission(); } catch (e) {} }
      return { alarms: a };
    }
    if (p === "/api/config/ics") fail("웹 버전에서는 구글 캘린더 연동을 아직 지원하지 않아요");
    if (p === "/api/notify/test") {
      const msg = (await alarmMessage(body.kind || "morning")) || ["보낼 알림이 없어요", ["오늘 마감·지연 업무가 없어요."]];
      if (!notify(...msg)) fail("브라우저 알림 권한을 허용해 주세요 (주소창 왼쪽 자물쇠 → 알림 허용)");
      return { title: msg[0], lines: msg[1] };
    }
    fail("없는 주소");
  }

  const WBCore = {
    PALETTE, STATUSES,
    init(adapter) {
      A = adapter; cache.clear();
      const s = lsGet("wb.settings", null);
      if (s) settings = { ...settings, ...s };
      startAlarms();
    },
    setCalendar(provider) { CAL = provider || null; },
    setVault(name) { settings.vault = name || ""; lsSet("wb.settings", settings); },
    get vault() { return settings.vault; },
    handle(path, body) {
      const run = chain.then(() => route(path, body));
      chain = run.catch(() => {});
      return run;
    },
    // 테스트용
    _internals: { readNote, setFrontmatter, removeFrontmatter, appendLog, editCheckLine, addCheckLine },
  };
  root.WBCore = WBCore;
  if (typeof module !== "undefined" && module.exports) module.exports = WBCore;
})(typeof window !== "undefined" ? window : globalThis);
