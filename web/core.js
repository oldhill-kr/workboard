/* 콕핏(구 업무보드) 웹 코어: server.py가 하던 일(노트 읽기/쓰기, 보관, 이름 변경, 색 고정 …)을 브라우저에서 한다.
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
  function addCheckLine(text, item, done = false) {
    const lines = text.split("\n"), nw = `- [${done ? "x" : " "}] ${item}`;
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
      id: name, title, project: fm.project || "기타", status: fm.status || "할일",
      priority: fm.priority || (fm.board === "차트" ? "한국" : "보통"), board: fm.board || "업무", symbol: String(fm.symbol || ""),
      themes: (() => { const p = fm.project || "기타", th = (Array.isArray(fm.themes) ? fm.themes : []).map(String).filter(Boolean); return th[0] === p ? th : [p, ...th.filter(x => x !== p)]; })(),
      due: fm.due || null, created: fm.created || null, completed: fm.completed || null,
      archived: String(fm.archived || "").toLowerCase() === "true", recurring: fm.recurring || "",
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
  async function listTasks(includeArchived = false, charts = false) {  // 차트보드 카드(board: 차트)는 charts일 때만
    await syncNotes();
    const out = [];
    for (const [path, c] of [...cache.entries()].sort((a, b) => a[0].localeCompare(b[0]))) {
      if (!path.startsWith(TASK_DIR + "/")) continue;
      try {
        const t = taskFromNote(path.slice(TASK_DIR.length + 1), c.text, c.mtime);
        if (t && (includeArchived || !t.archived) && (charts || t.board !== "차트")) out.push(t);
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
  async function moveCheck(id, mv) {
    const idxs = [...new Set((mv.idxs != null ? mv.idxs : [mv.idx]).map(Number))].sort((a, b) => a - b), to = (mv.to || "").trim();
    if (!to || to === id) fail("옮길 업무를 골라 주세요");
    const sp = taskPath(id), tp = taskPath(to);
    const stext = norm(cache.get(sp).text), ttext = norm(cache.get(tp).text);
    let n = -1;
    const found = {};
    for (const line of stext.split("\n")) {
      const cm = CHECK_RE.exec(line);
      if (cm) { n++; if (idxs.includes(n)) found[n] = { text: cm[4], done: cm[2] !== " " }; }
    }
    if (!idxs.length || Object.keys(found).length !== idxs.length) fail("할 일 항목을 찾지 못했어요. 새로고침 후 다시 해 주세요");
    let nt = ttext;
    for (const i of idxs) nt = addCheckLine(nt, found[i].text, found[i].done);  // 원래 순서대로 붙인다
    await putNote(tp, nt);  // 받는 쪽을 먼저 저장
    let ns = stext;
    for (const i of [...idxs].reverse()) ns = editCheckLine(ns, i, null);  // 뒤에서부터 지운다
    await putNote(sp, ns);
    return { task: taskFromNote(id, ns, cache.get(sp).mtime) };
  }
  async function updateTask(data) {
    await syncNotes();
    if (data.move_check != null) return moveCheck(data.id, data.move_check);
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
    if ("set_view" in data) text = setSectionBullets(text, "관점", [String(data.set_view || "").trim()].filter(Boolean));  // 차트보드 관점 메모
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

  function setSectionBullets(text, section, items) {
    const nl = text.includes("\r\n") ? "\r\n" : "\n";
    const lines = text.split(nl);
    const neu = items.map(i => `- ${oneline(i)}`);
    const start = lines.findIndex(l => l.trim() === `## ${section}`);
    if (start < 0) return neu.length ? text.replace(/\s+$/, "") + nl + nl + `## ${section}` + nl + neu.join(nl) + nl : text;
    let end = lines.findIndex((l, i) => i > start && l.startsWith("## "));
    if (end < 0) end = lines.length;
    lines.splice(start + 1, end - start - 1, ...neu, ...(end < lines.length ? [""] : []));
    return lines.join(nl);
  }

  async function createTask(data) {
    const title = (data.title || "").trim();
    if (!title) fail("업무 이름이 필요해요");
    const chart = data.board === "차트";
    const themes = chart ? String(data.project || "").split(/[,，/·]/).map(x => x.trim().replace(/[\\/:*?"<>|#^\[\]]/g, " ").trim()).filter((x, i, a) => x && a.indexOf(x) === i) : [];
    const project = (themes[0] || (data.project || (chart ? "미분류" : "기타"))).trim();
    await syncNotes();
    if (!chart && (await archivedProjects()).has(project)) await setArchived(project, false); // 보관된 프로젝트에 추가하면 다시 꺼낸다
    const name = `${project} - ${title}`.replace(/[\\/:*?"<>|#^\[\]]/g, " ").trim();
    let fname = `${name}.md`, i = 2;
    while (cache.has(`${TASK_DIR}/${fname}`)) fname = `${name} (${i++}).md`;
    const today = todayISO(), stem = fname.replace(/\.md$/, "");
    const checks = (data.checks || "").split("\n").map(c => c.trim()).filter(c => c);
    const checkLines = checks.map(c => `- [ ] ${c}`).join("\n") || "- [ ] ";
    const rec = (data.recurring || "").trim();  // 정기 업무 규칙이 만든 노트는 규칙 id를 머리말에 남긴다
    const text = `---
type: 업무
project: ${project}
status: ${data.status || "할일"}
priority: ${data.priority || (chart ? "한국" : "보통")}
due: ${data.due || ""}
created: ${today}
${chart ? "board: 차트\n" + (themes.length > 1 ? `themes: [${themes.join(", ")}]\n` : "") : ""}${chart && data.symbol ? `symbol: ${String(data.symbol).replace(/[^0-9A-Za-z.\-]/g, "")}\n` : ""}people: []
${rec ? `recurring: ${rec}\n` : ""}tags: ${chart ? "[work, 차트]" : "[work, 업무]"}
---

# ${stem}

## 할 일
${checkLines}

## 진행 기록
- ${today}: 등록 (${rec ? "정기 업무" : chart ? "차트보드" : "업무보드"})
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
    const names = new Set((await listTasks(true, true)).map(t => t.project));
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

  // ---------- 이슈 노트 (Work/이슈) · 대기 경과일 ----------
  const ISSUE_DIR = "이슈", OPEN_ISSUE_STATUS = ["미해결", "확인필요"];
  async function listIssues() {
    const out = [];
    for (const f of (await A.list(ISSUE_DIR)).filter(x => x.name.endsWith(".md"))) {
      try {
        const text = await A.read(`${ISSUE_DIR}/${f.name}`);
        if (text == null) continue;
        const { fm, body } = readNote(text), stem = f.name.replace(/\.md$/, "");
        let title = stem;
        for (const l of body.split("\n")) if (l.startsWith("# ")) { title = l.slice(2).trim(); break; }
        let obsidian = null;
        if (A.obsidian && settings.vault)
          obsidian = `obsidian://open?vault=${encodeURIComponent(settings.vault)}&file=${encodeURIComponent((A.folderName || "Work") + "/" + ISSUE_DIR + "/" + stem)}`;
        out.push({ id: f.name, stem, title, date: fm.date || "", status: fm.status || "", project: fm.project || "",
          category: fm.category || "", equipment: fm.equipment || "", mtime: f.mtime, obsidian });
      } catch (e) { console.warn("이슈 노트 읽기 실패", f.name, e); }
    }
    return out.sort((a, b) => b.date.localeCompare(a.date) || b.mtime - a.mtime);
  }
  const WAIT_LOG_RE = /^\s*[-*] (\d{4}-\d{2}-\d{2}): 상태 \S+ → 대기\s*$/;
  function waitSince(t) {  // '대기'로 바꾼 날짜 (진행 기록의 '상태 … → 대기' 줄). 기록이 없으면 노트를 마지막으로 고친 날
    let last = null;
    for (const line of norm(t.body).split("\n")) { const m = WAIT_LOG_RE.exec(line); if (m) last = m[1]; }
    return last || isoDate(new Date(t.mtime * 1000));
  }

  // ---------- 정기(반복) 업무 ----------
  const RECURRING_PATH = ".업무보드-정기업무.json";
  const WEEKDAY_KO = ["일", "월", "화", "수", "목", "금", "토"];  // 규칙의 요일 값: 0=일 … 6=토
  const parseISO = iso => new Date(iso + "T00:00:00");
  const addDays = (d, n) => { const x = new Date(d); x.setDate(x.getDate() + n); return x; };
  const isHolidayDate = d => (root.WB_HOLIDAYS || {})[isoDate(d)]?.kind === "holiday";
  async function loadRecurring() {
    const d = await readJson(RECURRING_PATH);
    return { items: d && Array.isArray(d.items) ? d.items : [] };
  }
  async function saveRecurringList(data) {
    const old = new Map((await loadRecurring()).items.map(r => [r.id, r]));
    const out = [], seen = new Set(), today = todayISO();
    for (const it of data.items || []) {
      const title = (it.title || "").trim();
      if (!title) continue;
      const freq = it.freq === "monthly" ? "monthly" : "weekly";
      const when = String(it.when ?? "").trim();
      if (when !== "last") {
        const n = Number(when), ok = /^\d+$/.test(when) && (freq === "weekly" ? n >= 0 && n <= 6 : n >= 1 && n <= 31);
        if (!ok) fail("요일 또는 날짜가 올바르지 않아요");
      }
      let rid = String(it.id || "");
      if (!rid || seen.has(rid)) rid = "g" + Math.random().toString(16).slice(2, 10).padEnd(8, "0");
      seen.add(rid);
      const prev = old.get(rid) || {};
      out.push({ id: rid, title: title.slice(0, 80), project: (it.project || "").trim() || "기타",
        priority: ["높음", "보통", "낮음"].includes(it.priority) ? it.priority : "보통", freq, when,
        checks: (it.checks || "").trim(), since: prev.since || today, last_period: prev.last_period || "" });
    }
    await A.write(RECURRING_PATH, JSON.stringify({ items: out }, null, 1));
    return { items: out };
  }
  function lastWorkday(start, end) {
    for (let d = new Date(end); d >= start; d = addDays(d, -1)) {
      const w = d.getDay();
      if (w !== 0 && w !== 6 && !isHolidayDate(d)) return d;
    }
    return end;
  }
  function recurringOccurrence(rule, today) {  // 오늘이 속한 주기(주/월)의 [주기 키, 발생 날짜]
    if (rule.freq === "monthly") {
      const y = today.getFullYear(), m = today.getMonth(), lastDay = new Date(y, m + 1, 0).getDate();
      const occ = rule.when === "last" ? lastWorkday(new Date(y, m, 1), new Date(y, m, lastDay)) : new Date(y, m, Math.min(Number(rule.when), lastDay));
      return [`${y}-${pad(m + 1)}`, occ];
    }
    const ws = addDays(today, -((today.getDay() + 6) % 7));
    const occ = rule.when === "last" ? lastWorkday(ws, addDays(ws, 4)) : addDays(ws, (Number(rule.when) + 6) % 7);
    return [isoWeekKeyOf(today), occ];
  }
  async function ensureRecurring() {  // 이번 주기의 정기 업무가 아직 없으면 만든다 (규칙마다 주기당 한 번)
    const doc = await loadRecurring();
    if (!doc.items.length) return 0;
    const today = parseISO(todayISO());
    const archived = await archivedProjects();
    const existing = new Set((await listTasks(true)).filter(t => t.recurring).map(t => t.recurring + "|" + t.due));
    let made = 0, changed = false;
    for (const r of doc.items) {
      let key, occ;
      try { [key, occ] = recurringOccurrence(r, today); } catch (e) { continue; }
      const occISO = isoDate(occ);
      if (r.last_period === key || occISO < (r.since || "") || archived.has(r.project)) continue;
      if (!existing.has(r.id + "|" + occISO)) {
        await createTask({ title: `${r.title} (${pad(occ.getMonth() + 1)}-${pad(occ.getDate())})`, project: r.project || "기타",
          due: occISO, priority: r.priority || "보통", checks: r.checks || "", recurring: r.id });
        made++;
      }
      r.last_period = key; changed = true;
    }
    if (changed) {
      const fresh = await loadRecurring(), keys = new Map(doc.items.map(r => [r.id, r.last_period]));
      for (const r of fresh.items) if (keys.get(r.id)) r.last_period = keys.get(r.id);
      await A.write(RECURRING_PATH, JSON.stringify(fresh, null, 1));
    }
    return made;
  }

  // ---------- 주간 노트 · 보고 ----------
  const WEEKLY_DIR = "주간", WEEKLY_BEGIN = "<!-- 업무보드:자동 시작 -->", WEEKLY_END = "<!-- 업무보드:자동 끝 -->";
  function weekBounds(week) {  // '2026-W42' (없으면 이번 주) → { start, end, key }
    let start;
    if (week) {
      const m = /^(\d{4})-W(\d{2})$/.exec(week.trim());
      if (!m) fail("주 형식은 2026-W42");
      const jan4 = new Date(Number(m[1]), 0, 4);
      start = addDays(jan4, -((jan4.getDay() + 6) % 7) + (Number(m[2]) - 1) * 7);
    } else {
      const t = parseISO(todayISO());
      start = addDays(t, -((t.getDay() + 6) % 7));
    }
    return { start, end: addDays(start, 6), key: isoWeekKeyOf(start) };
  }
  const mdDay = iso => iso.slice(5).replace("-", "/");
  function weekTitle(start, end) {
    const th = addDays(start, 3);  // 목요일이 속한 달의 몇 번째 주 (ISO 방식)
    const f = d => `${pad(d.getMonth() + 1)}/${pad(d.getDate())}`;
    return `${th.getMonth() + 1}월 ${Math.floor((th.getDate() - 1) / 7) + 1}주차 (${f(start)} ~ ${f(end)})`;
  }
  const ruleLabel = r => r.freq === "monthly"
    ? "매월 " + (r.when === "last" ? "마지막 근무일" : `${Number(r.when)}일`)
    : "매주 " + (r.when === "last" ? "마지막 근무일" : WEEKDAY_KO[Number(r.when)] + "요일");
  async function buildWeekly(week) {
    const { start, end, key } = weekBounds(week);
    const s = isoDate(start), e = isoDate(end), todayD = parseISO(todayISO()), today = isoDate(todayD);
    const personal = new Set(await personalList());  // 개인(성장) 프로젝트는 업무 보고에서 뺀다
    const ts = (await listTasks(true)).filter(t => !personal.has(t.project));
    const ref = today < e ? todayD : end;  // 지난 주는 그 주 일요일, 이번 주는 오늘 기준으로 지연을 센다
    const days = (a, b) => Math.round((a - b) / 86400000);
    const done = ts.filter(t => t.status === "완료" && s <= (t.completed || "") && (t.completed || "") <= e).sort((a, b) => a.completed.localeCompare(b.completed));
    const doing = ts.filter(t => (t.status === "진행중" || t.status === "할일") && !t.archived && (t.status === "진행중" || (t.due && t.due <= e)))
      .sort((a, b) => (a.due || "9999").localeCompare(b.due || "9999"));
    const waiting = ts.filter(t => t.status === "대기" && !t.archived).sort((a, b) => waitSince(a).localeCompare(waitSince(b)));
    const nxtTo = isoDate(addDays(end, 7));
    const nxt = ts.filter(t => (t.status === "진행중" || t.status === "할일") && !t.archived && t.due && e < t.due && t.due <= nxtTo)
      .sort((a, b) => a.due.localeCompare(b.due));
    const issues = await listIssues();
    const newIssues = issues.filter(i => s <= i.date && i.date <= e);
    const openIssues = issues.filter(i => OPEN_ISSUE_STATUS.includes(i.status));
    const rules = (await loadRecurring()).items;
    const short = t => t.title.replace(t.project + " - ", "");
    const whenOf = t => {
      if (!t.due) return "마감 미정";
      const late = days(ref, parseISO(t.due));
      return `마감 ${mdDay(t.due)}` + (late > 0 ? ` · 지연 ${late}일` : "");
    };
    const waited = t => `${Math.max(0, days(todayD, parseISO(waitSince(t))))}일째 대기`;
    const stemOf = t => t.id.replace(/\.md$/, "");
    const md = [`## 📊 업무 요약 (자동 생성 · ${mdDay(today)} 기준)`,
      `- 완료 ${done.length} · 진행·이월 ${doing.length} · 대기 ${waiting.length} · 다음 주 예정 ${nxt.length} · 미해결 이슈 ${openIssues.length}`, ""];
    const block = (title, rows, fmt) => { if (rows.length) { md.push(`### ${title} (${rows.length})`, ...rows.map(fmt), ""); } };
    block("✅ 이번 주 완료", done, t => `- [[${stemOf(t)}]] · 완료 ${mdDay(t.completed)}`);
    block("🔧 진행·이월", doing, t => `- [[${stemOf(t)}]] · ${t.status} · ${whenOf(t)}`);
    block("❓ 대기·확인", waiting, t => `- [[${stemOf(t)}]] · ${waited(t)}`);
    block("📅 다음 주 예정", nxt, t => `- [[${stemOf(t)}]] · 마감 ${mdDay(t.due)}`);
    block("🐞 이번 주 등록 이슈", newIssues, i => `- [[${i.stem}]] · ${i.status || "-"}`);
    block("🚧 미해결 이슈", openIssues, i => `- [[${i.stem}]] · ${i.status} · ${i.date}`);
    block("🔁 정기 업무", rules, r => `- ${r.title} · ${ruleLabel(r)}`);
    const markdown = md.join("\n").replace(/\s+$/, "") + "\n";

    const tx = [`[주간 업무 보고] ${key} · ${weekTitle(start, end)}`];
    const tblock = (title, rows, fmt) => { if (rows.length) tx.push("", `■ ${title} (${rows.length})`, ...rows.map(r => ` - ${fmt(r)}`)); };
    tblock("이번 주 완료", done, t => `[${t.project}] ${short(t)}`);
    tblock("진행·이월", doing, t => `[${t.project}] ${short(t)} (${whenOf(t)})`);
    tblock("대기·확인", waiting, t => `[${t.project}] ${short(t)} (${waited(t)})`);
    tblock("다음 주 예정", nxt, t => `[${t.project}] ${short(t)} (마감 ${mdDay(t.due)})`);
    tblock("미해결 이슈", openIssues, i => `${i.title} (${i.status})`);
    if (tx.length === 1) tx.push("(이번 주에 정리할 업무가 없어요)");
    const note = `${WEEKLY_DIR}/${key}.md`;
    return { week: key, start: s, end: e, title: weekTitle(start, end), note, note_exists: (await A.read(note)) != null,
      counts: { done: done.length, doing: doing.length, waiting: waiting.length, next: nxt.length, issues: openIssues.length },
      markdown, text: tx.join("\n") + "\n" };
  }
  async function saveWeekly(data) {  // 이미 있는 노트는 자동 구간만 바꾸고 손으로 쓴 부분은 건드리지 않는다
    const r = await buildWeekly((data || {}).week);
    const block = `${WEEKLY_BEGIN}\n${r.markdown.replace(/\s+$/, "")}\n${WEEKLY_END}`;
    const raw = await A.read(r.note);
    if (raw == null) {
      await A.write(r.note, `---\ntype: 주간\nweek: ${r.week}\nstart: ${r.start}\nend: ${r.end}\ntags: [work, 주간]\n---\n\n# ${r.title}\n\n${block}\n\n## 회고 (주말 전에)\n- \n`);
      return { path: r.note, created: true };
    }
    const nl = raw.includes("\r\n") ? "\r\n" : "\n";
    let text = norm(raw);
    const i = text.indexOf(WEEKLY_BEGIN), j = text.indexOf(WEEKLY_END);
    text = i >= 0 && j > i ? text.slice(0, i) + block + text.slice(j + WEEKLY_END.length) : text.replace(/\n+$/, "") + "\n\n" + block + "\n";
    await A.write(r.note, nl === "\n" ? text : text.replace(/\n/g, nl));
    return { path: r.note, created: false };
  }

  // ---------- 알림 (브라우저 탭이 열려 있을 때만) ----------
  const URGENT_MARK = "🔥";
  function urgentItems(tasks) {
    const out = [];
    for (const t of tasks) {
      if (t.status === "완료" || t.status === "보류") continue;
      t.checks.forEach((c, i) => { if (!c.done && c.text.trimStart().startsWith(URGENT_MARK)) out.push({ t, i, text: c.text.trimStart().slice(URGENT_MARK.length).trim() }); });
    }
    return out.sort((a, b) => (a.t.due || "9999").localeCompare(b.t.due || "9999"));
  }
  const urgentLines = (items, n = 3) => items.slice(0, n).map(x => `${x.t.project} · ${x.text}`).join(", ") + (items.length > n ? ` 외 ${items.length - n}건` : "");
  async function alarmMessage(kind) {
    const today = todayISO(), tomorrow = addDaysISO(today, 1);
    const all = await listTasks();
    const urg = urgentItems(all);
    if (kind === "urgent") return urg.length ? [`🔥 긴급 ${urg.length}건 — 먼저 처리하세요`, [urgentLines(urg)]] : null;
    const open = all.filter(t => t.status !== "완료" && t.status !== "보류");
    const late = open.filter(t => t.due && t.due < today).sort((a, b) => a.due.localeCompare(b.due));
    const dueToday = open.filter(t => t.due === today), dueTom = open.filter(t => t.due === tomorrow);
    const short = t => t.title.replace(t.project + " - ", "");
    if (kind === "morning") {
      const names = [...new Set([...dueToday, ...late].map(short))];
      return [`좋은 아침이에요 ☀ 지연 ${late.length} · 오늘 마감 ${dueToday.length} · 내일 마감 ${dueTom.length}`,
        [...(urg.length ? [`🔥 긴급 ${urg.length}건: ` + urgentLines(urg, 2)] : []),
         names.length ? "✅ " + names.slice(0, 3).join(", ") + (names.length > 3 ? ` 외 ${names.length - 3}건` : "") : "오늘 마감인 업무가 없어요."]];
    }
    if (kind === "evening") {
      if (!dueToday.length && !late.length && !urg.length) return null;
      return [`퇴근 전 체크 — 못 끝낸 업무 ${dueToday.length + late.length}건`,
        [(urg.length ? `🔥 긴급 ${urg.length}건: ` + urgentLines(urg, 2) : ""), (dueToday.length ? "오늘 마감: " + dueToday.slice(0, 3).map(short).join(", ") : ""), (late.length ? `지연 ${late.length}건: ` + late.slice(0, 2).map(short).join(", ") : "")].filter(x => x)];
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
  // 알림은 업무보드 화면 안의 큰 팝업(WB_ALERT)으로 띄우고, 탭이 가려져 있으면 브라우저 알림도 함께 보낸다
  function notify(title, lines, kind) {
    let shown = false;
    if (typeof root.WB_ALERT === "function") { root.WB_ALERT({ kind: kind || "info", title, lines: lines.filter(Boolean) }); shown = true; }
    if (typeof Notification === "undefined") return shown;
    if (Notification.permission !== "granted") { if (!shown) { try { Notification.requestPermission(); } catch (e) {} } return shown; }
    if (typeof document !== "undefined" && document.visibilityState === "visible" && shown) return true;
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
            if (msg) notify(msg[0], msg[1], kind);
          }
        }
        // 긴급 할 일: 평일 근무 시간에 urgent_every분마다 반복 알림 (0이면 끔)
        const ev = Number(settings.urgent_every ?? 60), wd2 = now.getDay();
        const Hh = root.WB_HOLIDAYS || {};
        if (ev > 0 && wd2 !== 0 && wd2 !== 6 && Hh[isoDate(now)]?.kind !== "holiday" && hm >= "09:00" && hm <= "18:00") {
          const last = Number(lsGet("wb.urgentLast", 0));
          if (Date.now() - last >= ev * 60000) {
            lsSet("wb.urgentLast", Date.now());
            const msg = await alarmMessage("urgent");
            if (msg) notify(msg[0], msg[1], "urgent");
          }
        }
      } catch (e) { console.warn("알림 오류", e); }
    }, 30000);
  }

  // ---------- 라우터 ----------
  async function route(path, body) {
    const u = new URL(path, "http://x"), p = u.pathname, q = u.searchParams;
    if (!body) {
      if (p === "/api/issues") return { issues: await listIssues() };
      if (p === "/api/recurring") return loadRecurring();
      if (p === "/api/weekly") return buildWeekly(q.get("week") || "");
      if (p === "/api/tasks") {
        try { await ensureRecurring(); } catch (e) { console.warn("정기 업무 생성 실패", e); }  // 이번 주기의 정기 업무가 없으면 만든다
      }
      if (p === "/api/tasks") return { tasks: await listTasks(false, true), archived: await archivedSummary(), colors: await projectColors(), today: todayISO() };
      if (p === "/api/events") {
        if (!CAL) return { events: [], errors: [] };
        try { return await CAL.fetch(q.get("start") || todayISO(), q.get("end") || addDaysISO(todayISO(), 42)); }
        catch (e) { return { events: [], errors: [e.message] }; }
      }
      if (p === "/api/holidays") return listHolidays(q.get("start") || "0000-01-01", q.get("end") || "9999-12-31");
      if (p === "/api/growth") return listGrowth();
      if (p === "/api/routines") return loadRoutines();
      if (p === "/api/config") return { ics_count: CAL ? 1 : 0, work_dir: A ? A.label : "", alarms: settings.alarms, urgent_every: settings.urgent_every ?? 60, static: true };
      fail("없는 주소");
    }
    if (p === "/api/recurring/list") return saveRecurringList(body);
    if (p === "/api/weekly/save") return saveWeekly(body);
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
      settings.alarms = a;
      if ("urgent_every" in body) {
        const ue = Number(body.urgent_every || 0);
        if (!(ue >= 0 && ue <= 1440)) fail("긴급 알림 간격은 0~1440분");
        settings.urgent_every = Math.floor(ue);
      }
      lsSet("wb.settings", settings);
      if (Object.values(a).some(v => v) && typeof Notification !== "undefined" && Notification.permission === "default") { try { Notification.requestPermission(); } catch (e) {} }
      return { alarms: a };
    }
    if (p === "/api/config/ics") fail("웹 버전에서는 구글 캘린더 연동을 아직 지원하지 않아요");
    if (p === "/api/notify/test") {
      const msg = (await alarmMessage(body.kind || "morning")) || ["보낼 알림이 없어요", ["오늘 마감·지연 업무가 없어요."]];
      if (!notify(msg[0], msg[1], body.kind || "morning")) fail("브라우저 알림 권한을 허용해 주세요 (주소창 왼쪽 자물쇠 → 알림 허용)");
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
