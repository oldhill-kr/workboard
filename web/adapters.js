/* 저장소 어댑터 3종: 로컬(브라우저) / 구글 드라이브 / 옵시디언(내 PC 폴더)
 * 공통 인터페이스: list(dir), listAll(), read(path), write(path, text), remove(path), label
 */
(function (root) {
  const isMd = n => n.endsWith(".md");
  const hidden = n => n.startsWith(".") || n === "_휴지통";

  // ---------- IndexedDB 도우미 ----------
  function idb() {
    return new Promise((res, rej) => {
      const r = indexedDB.open("workboard", 1);
      r.onupgradeneeded = () => { r.result.createObjectStore("files", { keyPath: "path" }); r.result.createObjectStore("meta"); };
      r.onsuccess = () => res(r.result);
      r.onerror = () => rej(r.error);
    });
  }
  const tx = (db, store, mode, fn) => new Promise((res, rej) => {
    const t = db.transaction(store, mode), s = t.objectStore(store), out = fn(s);
    t.oncomplete = () => res(out && out.result !== undefined ? out.result : undefined);
    t.onerror = () => rej(t.error);
  });
  const meta = {
    async get(k) { const db = await idb(); return tx(db, "meta", "readonly", s => s.get(k)); },
    async set(k, v) { const db = await idb(); return tx(db, "meta", "readwrite", s => s.put(v, k)); },
  };

  // ---------- A. 로컬 (이 브라우저의 IndexedDB) ----------
  function localAdapter() {
    return {
      kind: "local", label: "이 브라우저 (로컬)",
      async list(dir) {
        const db = await idb(), all = await tx(db, "files", "readonly", s => s.getAll());
        return all.filter(f => f.path.startsWith(dir + "/") && !f.path.slice(dir.length + 1).includes("/"))
          .map(f => ({ name: f.path.slice(dir.length + 1), mtime: f.mtime }));
      },
      async listAll() {
        const db = await idb(), all = await tx(db, "files", "readonly", s => s.getAll());
        return all.map(f => f.path).filter(p => isMd(p) && !p.split("/").some(hidden));
      },
      async read(path) { const db = await idb(); const f = await tx(db, "files", "readonly", s => s.get(path)); return f ? f.text : null; },
      async write(path, text) { const db = await idb(); await tx(db, "files", "readwrite", s => s.put({ path, text, mtime: Date.now() / 1000 })); },
      async remove(path) { const db = await idb(); await tx(db, "files", "readwrite", s => s.delete(path)); },
      async exportAll() { const db = await idb(); return tx(db, "files", "readonly", s => s.getAll()); },
      async importAll(files) { const db = await idb(); await tx(db, "files", "readwrite", s => { for (const f of files) s.put(f); }); },
    };
  }

  // ---------- C. 옵시디언 / 내 PC 폴더 (File System Access API) ----------
  function folderAdapter(dirHandle) {
    async function dirOf(parts, create) {
      let d = dirHandle;
      for (const p of parts) d = await d.getDirectoryHandle(p, { create });
      return d;
    }
    const split = path => { const a = path.split("/"); return [a.slice(0, -1), a[a.length - 1]]; };
    return {
      kind: "folder", label: "폴더: " + dirHandle.name, obsidian: true, folderName: dirHandle.name,
      async list(dir) {
        let d;
        try { d = await dirOf(dir.split("/"), false); } catch (e) { return []; }
        const out = [];
        for await (const [name, h] of d.entries()) {
          if (h.kind !== "file") continue;
          const f = await h.getFile();
          out.push({ name, mtime: f.lastModified / 1000 });
        }
        return out;
      },
      async listAll() {
        const out = [];
        async function walk(d, prefix) {
          for await (const [name, h] of d.entries()) {
            if (hidden(name)) continue;
            if (h.kind === "directory") await walk(h, prefix + name + "/");
            else if (isMd(name)) out.push(prefix + name);
          }
        }
        await walk(dirHandle, "");
        return out;
      },
      async read(path) {
        const [dirs, name] = split(path);
        try { const d = await dirOf(dirs, false); const h = await d.getFileHandle(name); return await (await h.getFile()).text(); }
        catch (e) { if (e.name === "NotFoundError" || e.name === "TypeMismatchError") return null; throw e; }
      },
      async write(path, text) {
        const [dirs, name] = split(path);
        const d = await dirOf(dirs, true), h = await d.getFileHandle(name, { create: true });
        const w = await h.createWritable(); await w.write(text); await w.close();
      },
      async remove(path) {
        const [dirs, name] = split(path);
        try { const d = await dirOf(dirs, false); await d.removeEntry(name); } catch (e) { if (e.name !== "NotFoundError") throw e; }
      },
    };
  }
  async function pickFolder() {
    if (!window.showDirectoryPicker) throw new Error("이 브라우저는 폴더 접근을 지원하지 않아요. Chrome 또는 Edge에서 열어 주세요.");
    let h = await window.showDirectoryPicker({ mode: "readwrite", id: "workboard" });
    // 볼트 루트를 골랐다면 Work 폴더로 한 단계 내려간다
    try {
      await h.getDirectoryHandle("업무");
    } catch (e) {
      try { const w = await h.getDirectoryHandle("Work"); await w.getDirectoryHandle("업무"); h = w; } catch (e2) { /* 새 폴더: 업무/ 는 처음 저장할 때 만든다 */ }
    }
    await meta.set("folder", h);
    return h;
  }
  async function savedFolder() { return (await meta.get("folder")) || null; }
  async function ensurePermission(h) {
    const o = { mode: "readwrite" };
    if ((await h.queryPermission(o)) === "granted") return true;
    return (await h.requestPermission(o)) === "granted";
  }

  // ---------- B. 구글 드라이브 (drive.file 범위: 이 앱이 만든 파일만 접근) ----------
  const GIS = "https://accounts.google.com/gsi/client";
  const SCOPE = "https://www.googleapis.com/auth/drive.file";
  const FOLDER_NAME = "업무보드 데이터";
  function loadScript(src) {
    return new Promise((res, rej) => {
      if (document.querySelector(`script[src="${src}"]`)) return res();
      const s = document.createElement("script"); s.src = src; s.onload = res; s.onerror = () => rej(new Error("구글 로그인 스크립트를 불러오지 못했어요"));
      document.head.appendChild(s);
    });
  }
  async function driveAdapter(clientId) {
    await loadScript(GIS);
    let token = null, expires = 0, waiting = null;
    function requestToken(prompt) {
      return new Promise((res, rej) => {
        const c = google.accounts.oauth2.initTokenClient({
          client_id: clientId, scope: SCOPE,
          callback: r => { if (r.error) return rej(new Error("구글 로그인에 실패했어요: " + r.error)); token = r.access_token; expires = Date.now() + (r.expires_in - 60) * 1000; res(); },
          error_callback: e => rej(new Error("구글 로그인 창이 닫혔어요 (" + (e.type || "취소") + ")")),
        });
        c.requestAccessToken({ prompt });
      });
    }
    async function auth() {
      if (token && Date.now() < expires) return token;
      if (!waiting) waiting = requestToken(token ? "" : "consent").finally(() => { waiting = null; });
      await waiting;
      return token;
    }
    async function call(url, opts = {}) {
      const t = await auth();
      const r = await fetch(url, { ...opts, headers: { ...(opts.headers || {}), Authorization: "Bearer " + t } });
      if (r.status === 401) { token = null; return call(url, opts); }
      if (!r.ok) throw new Error(`구글 드라이브 오류 (${r.status})`);
      return r;
    }
    await auth();
    // 앱 전용 폴더 찾기/만들기
    const q = encodeURIComponent(`name='${FOLDER_NAME}' and mimeType='application/vnd.google-apps.folder' and trashed=false`);
    let rootId = (await (await call(`https://www.googleapis.com/drive/v3/files?q=${q}&fields=files(id)`)).json()).files[0]?.id;
    if (!rootId) {
      rootId = (await (await call("https://www.googleapis.com/drive/v3/files", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name: FOLDER_NAME, mimeType: "application/vnd.google-apps.folder" }),
      })).json()).id;
    }
    let index = new Map(), indexAt = 0;  // path → {id, mtime}
    async function refresh(force) {
      if (!force && Date.now() - indexAt < 15000) return;
      const m = new Map();
      let page = "";
      do {
        const qq = encodeURIComponent(`'${rootId}' in parents and trashed=false`);
        const j = await (await call(`https://www.googleapis.com/drive/v3/files?q=${qq}&pageSize=1000&fields=nextPageToken,files(id,name,modifiedTime)${page ? "&pageToken=" + page : ""}`)).json();
        for (const f of j.files) m.set(f.name, { id: f.id, mtime: Date.parse(f.modifiedTime) / 1000 });
        page = j.nextPageToken || "";
      } while (page);
      index = m; indexAt = Date.now();
    }
    return {
      kind: "drive", label: "구글 드라이브",
      async list(dir) {
        await refresh();
        return [...index.entries()].filter(([p]) => p.startsWith(dir + "/") && !p.slice(dir.length + 1).includes("/"))
          .map(([p, v]) => ({ name: p.slice(dir.length + 1), mtime: v.mtime }));
      },
      async listAll() { await refresh(true); return [...index.keys()].filter(p => isMd(p) && !p.split("/").some(hidden)); },
      async read(path) {
        await refresh();
        const f = index.get(path);
        if (!f) return null;
        return (await call(`https://www.googleapis.com/drive/v3/files/${f.id}?alt=media`)).text();
      },
      async write(path, text) {
        await refresh();
        const f = index.get(path), type = path.endsWith(".json") ? "application/json" : "text/markdown";
        if (f) {
          await call(`https://www.googleapis.com/upload/drive/v3/files/${f.id}?uploadType=media`, { method: "PATCH", headers: { "Content-Type": type + "; charset=UTF-8" }, body: text });
          f.mtime = Date.now() / 1000;
        } else {
          const b = "wbboundary" + Math.random().toString(16).slice(2);
          const body = `--${b}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n${JSON.stringify({ name: path, parents: [rootId] })}\r\n--${b}\r\nContent-Type: ${type}; charset=UTF-8\r\n\r\n${text}\r\n--${b}--`;
          const j = await (await call("https://www.googleapis.com/upload/drive/v3/files?uploadType=multipart&fields=id", {
            method: "POST", headers: { "Content-Type": `multipart/related; boundary=${b}` }, body })).json();
          index.set(path, { id: j.id, mtime: Date.now() / 1000 });
        }
      },
      async remove(path) {
        await refresh();
        const f = index.get(path);
        if (!f) return;
        await call(`https://www.googleapis.com/drive/v3/files/${f.id}`, { method: "DELETE" });
        index.delete(path);
      },
    };
  }

  // ---------- 구글 캘린더 (읽기 전용) ----------
  const SCOPE_CAL = "https://www.googleapis.com/auth/calendar.readonly";
  function calendarProvider(clientId) {
    let token = null, expires = 0;
    function requestToken(prompt) {
      return new Promise((res, rej) => {
        const c = google.accounts.oauth2.initTokenClient({
          client_id: clientId, scope: SCOPE_CAL,
          callback: r => { if (r.error) return rej(new Error("구글 로그인에 실패했어요: " + r.error)); token = r.access_token; expires = Date.now() + (r.expires_in - 60) * 1000; res(); },
          error_callback: e => rej(new Error("구글 로그인 창이 닫혔거나 열 수 없어요 (" + (e.type || "취소") + ")")),
        });
        c.requestAccessToken({ prompt });
      });
    }
    async function ensure(prompt) {
      await loadScript(GIS);
      if (token && Date.now() < expires) return;
      await requestToken(prompt);
    }
    async function get(url) {
      const r = await fetch(url, { headers: { Authorization: "Bearer " + token } });
      if (r.status === 401) { token = null; throw new Error("구글 캘린더 연결이 만료됐어요. 설정에서 다시 연결해 주세요"); }
      if (!r.ok) throw new Error(`구글 캘린더 오류 (${r.status})`);
      return r.json();
    }
    const pad = n => String(n).padStart(2, "0");
    const localIso = d => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
    return {
      connect() { return ensure("consent"); },   // 사용자가 버튼을 눌렀을 때
      silent() { return ensure(""); },           // 이전에 허용한 경우 조용히 다시 연결
      async fetch(start, end) {
        await ensure("");
        const timeMin = new Date(start + "T00:00:00").toISOString(), timeMax = new Date(end + "T00:00:00").toISOString();
        const cals = (await get("https://www.googleapis.com/calendar/v3/users/me/calendarList?minAccessRole=reader")).items || [];
        const events = [], errors = [];
        for (const cal of cals) {
          if (cal.selected === false || /holiday@group\.v\.calendar\.google\.com$/.test(cal.id)) continue;  // 공휴일은 따로 표시
          try {
            const q = new URLSearchParams({ timeMin, timeMax, singleEvents: "true", orderBy: "startTime", maxResults: "250" });
            const j = await get(`https://www.googleapis.com/calendar/v3/calendars/${encodeURIComponent(cal.id)}/events?${q}`);
            for (const e of j.items || []) {
              if (e.status === "cancelled") continue;
              const allDay = !!(e.start && e.start.date);
              events.push({
                title: e.summary || "(제목 없음)",
                start: allDay ? e.start.date : localIso(new Date(e.start.dateTime)),
                end: allDay ? e.end.date : localIso(new Date(e.end.dateTime)),
                allDay, location: e.location || "", description: (e.description || "").slice(0, 500),
                calendar: cal.summaryOverride || cal.summary || "", calIndex: 0,
              });
            }
          } catch (err) { errors.push(`${cal.summary || cal.id}: ${err.message}`); }
        }
        return { events, errors };
      },
    };
  }

  root.WBAdapters = { localAdapter, folderAdapter, driveAdapter, calendarProvider, pickFolder, savedFolder, ensurePermission, meta };
  if (typeof module !== "undefined" && module.exports) module.exports = root.WBAdapters;
})(typeof window !== "undefined" ? window : globalThis);
