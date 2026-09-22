// Subject folders and their single aggregate note share the lecture archive in SQLite.
const subjectFolders = new Map();
const collapsedFolders = new Set();
let activeSubject = null;
let subjectRefresh = Promise.resolve();
let subjectSourceButtons = [];

async function libraryRequest(url, options = {}) {
  const response = await fetch(url, { signal: AbortSignal.timeout(10000), ...options });
  const data = await response.json();
  if (!response.ok) throw new Error(typeof data.detail === "string" ? data.detail : "노트 요청을 처리하지 못했습니다.");
  return data;
}

function acceptSubjects(folders) {
  subjectFolders.clear();
  for (const folder of folders) subjectFolders.set(folder.id, folder);
}

function refreshSubjects() {
  subjectRefresh = subjectRefresh.catch(() => {}).then(async () => {
    acceptSubjects(await libraryRequest("/api/subjects"));
    renderLibrary();
    renderNoteFolder();
  });
  return subjectRefresh;
}

function lectureButton(record) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = "saved-note";
  button.dataset.noteId = record.id;
  button.setAttribute("aria-current", String(record.id === activeNote?.id));
  button.disabled = busyStates.has(state);
  const title = document.createElement("strong");
  title.textContent = record.title;
  const meta = document.createElement("span");
  meta.textContent = `${noteDate(record.createdAt)}\n${record.has_final ? "최종 노트 완료" : "최종 노트 미완료"} · 청크 ${record.chunk_count}개`;
  button.appendChild(title);
  button.appendChild(meta);
  button.addEventListener("click", () => selectNote(record.id));
  return button;
}

function renderFolderLibrary() {
  const list = $("saved-notes");
  const scrollTop = list.scrollTop;
  list.replaceChildren();
  const groups = new Map([...subjectFolders.keys()].map((id) => [id, []]));
  const unfiled = [];
  for (const record of [...savedNotes.values()].sort((a, b) => b.createdAt.localeCompare(a.createdAt))) {
    (groups.get(record.subject_id) || unfiled).push(record);
  }
  for (const folder of subjectFolders.values()) {
    const box = document.createElement("details");
    box.className = "subject-folder";
    box.open = !collapsedFolders.has(folder.id);
    box.addEventListener("toggle", () => {
      if (!box.isConnected) return;
      if (box.open) collapsedFolders.delete(folder.id);
      else collapsedFolders.add(folder.id);
    });
    const heading = document.createElement("summary");
    heading.textContent = `${folder.name} · ${groups.get(folder.id).length}`;
    box.appendChild(heading);
    const overview = document.createElement("button");
    overview.type = "button";
    overview.className = "subject-note";
    overview.dataset.subjectId = folder.id;
    overview.textContent = "과목 종합 노트" + (folder.has_summary && folder.revision !== folder.summary_revision ? " · 갱신 필요" : "");
    overview.setAttribute("aria-current", String(activeSubject?.id === folder.id));
    overview.disabled = busyStates.has(state);
    overview.addEventListener("click", () => selectSubject(folder.id));
    box.appendChild(overview);
    for (const record of groups.get(folder.id)) box.appendChild(lectureButton(record));
    list.appendChild(box);
  }
  if (unfiled.length) {
    const group = document.createElement("section");
    group.className = "subject-folder";
    const title = document.createElement("h3");
    title.textContent = "미분류";
    group.appendChild(title);
    for (const record of unfiled) group.appendChild(lectureButton(record));
    list.appendChild(group);
  }
  list.scrollTop = scrollTop;
  $("note-count").textContent = String(savedNotes.size);
  $("library-empty").hidden = !!(savedNotes.size || subjectFolders.size);
  $("new-note").disabled = busyStates.has(state);
  $("subject-create").disabled = $("subject-name").disabled = busyStates.has(state);
  $("library-status").dataset.error = String(!!libraryWarning);
  $("library-status").textContent = libraryWarning || (busyStates.has(state)
    ? "작업이 끝나면 다른 노트를 선택할 수 있습니다."
    : "과목별 강의 노트와 종합 노트가 서버에 저장됩니다.");
}

function renderNoteFolder() {
  const select = $("note-folder");
  select.replaceChildren();
  for (const [value, label] of [["", "미분류"], ...[...subjectFolders.values()].map((folder) => [folder.id, folder.name])]) {
    const option = document.createElement("option");
    option.value = value;
    option.textContent = label;
    select.appendChild(option);
  }
  select.value = activeNote?.subject_id || "";
  $("note-folder-label").hidden = !activeNote || !!activeSubject;
  select.disabled = busyStates.has(state);
  $("note-delete").hidden = !activeNote || !!activeSubject;
  $("note-delete").disabled = busyStates.has(state);
}

function showLectureWorkspace() {
  activeSubject = null;
  $("subject-view").hidden = true;
  $("lecture-workspace").hidden = false;
  renderNoteFolder();
}

function initializeSubjectModels() {
  const select = $("subject-model");
  select.replaceChildren();
  for (const model of config.summary_models) {
    const option = document.createElement("option");
    option.value = model;
    option.textContent = model.replace("gpt-", "");
    select.appendChild(option);
  }
  select.value = config.final_model;
}

function traceDate(value) {
  return value ? new Date(value).toLocaleString("ko-KR", { year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit" }) : "기록 없음";
}

function traceElement(tag, className, text) {
  const element = document.createElement(tag);
  element.className = className;
  if (text !== undefined) element.textContent = text;
  return element;
}

function renderTraceSources(sources, subject) {
  const box = traceElement("div", "trace-sources");
  for (const source of sources) {
    const saved = (subject.source_notes || subject.notes).find((note) => note.id === source.note_id);
    const status = !saved ? " · 삭제된 강의" : saved.subject_id !== subject.id ? " · 다른 과목으로 이동됨" : saved.final_hash !== source.final_hash ? " · 이후 최종 노트 변경됨" : "";
    const button = traceElement("button", "trace-source", `${source.title} · 강의 ${traceDate(source.created_at)}${status}`);
    button.type = "button";
    button.title = `강의 ID: ${source.note_id}`;
    button.dataset.noteId = source.note_id;
    button.disabled = !saved || busyStates.has(state);
    button.addEventListener("click", () => selectNote(source.note_id));
    subjectSourceButtons.push({ button, available: !!saved });
    box.appendChild(button);
    box.appendChild(traceElement("blockquote", "trace-quote", source.quote));
    box.appendChild(traceElement("p", "trace-source-id", `강의 ID ${source.note_id} · 최종 요약 버전 ${source.final_hash.slice(0, 12)}`));
  }
  return box;
}

function renderTraceItem(item, subject) {
  const card = traceElement("article", "trace-item");
  card.dataset.itemId = item.id;
  card.appendChild(traceElement("p", "trace-text", item.text));
  card.appendChild(renderTraceDetails(item, subject));
  return card;
}

function renderTraceDetails(item, subject) {
  const last = item.history[item.history.length - 1];
  const sources = traceElement("details", "trace-details");
  sources.appendChild(traceElement("summary", "", `${item.id} · ${item.sources.length ? `출처 ${item.sources.length}개` : "직접 근거 미확인"} · 이력 ${item.history.length}건${item.active ? "" : " · 현재 노트에서 통합·제외됨"}`));
  sources.appendChild(traceElement("p", "trace-meta", `최초 추가 ${traceDate(item.created_at)} · 최근 반영 ${traceDate(item.updated_at)} · ${last.id}`));
  sources.appendChild(renderTraceSources(item.sources, subject));
  const history = traceElement("details", "trace-details trace-history");
  history.appendChild(traceElement("summary", "", `변경 이력 ${item.history.length}건`));
  let populated = false;
  history.addEventListener("toggle", () => {
    if (!history.open || populated) return;
    populated = true;
    const labels = { added: "추가", updated: "보완·출처 변경", removed: "제외", restored: "복원" };
    for (let index = item.history.length - 1; index >= 0; index--) {
      const event = item.history[index];
      const entry = traceElement("div", "trace-event");
      entry.appendChild(traceElement("strong", "", `${event.id} · ${labels[event.kind]} · ${event.part_id}/${item.id}`));
      entry.appendChild(traceElement("p", "trace-meta", `${traceDate(event.at)} · ${event.part_title} · ${event.model}`));
      if (index > 0 && item.history[index - 1].text !== event.text) {
        entry.appendChild(traceElement("p", "trace-text trace-before", `변경 전: ${item.history[index - 1].text}`));
      }
      entry.appendChild(traceElement("p", "trace-text", `${event.kind === "removed" ? "제외된 내용" : "반영 내용"}: ${event.text}`));
      entry.appendChild(renderTraceSources(event.sources, subject));
      history.appendChild(entry);
    }
  });
  sources.appendChild(history);
  return sources;
}

function renderSubjectSummary(subject) {
  const box = $("subject-summary");
  const document = subject.summary_document;
  subjectSourceButtons = [];
  box.replaceChildren();
  box.classList.toggle("tracked", !!document?.version);
  if (!document?.version) {
    box.textContent = subject.summary || "완료된 강의 최종 노트들을 통합해 이 과목의 핵심 개념과 흐름을 정리합니다.";
    if (subject.summary) box.appendChild(traceElement("p", "trace-meta", "다음 갱신부터 항목 ID·출처·추가 시각을 기록합니다. 기존 본문은 추적 이전 기록으로 보존됩니다."));
    return;
  }
  box.appendChild(traceElement("p", "trace-guide", "각 설명 아래의 출처·이력을 펼치면 근거 강의와 추가 시각, 변경 내용을 확인할 수 있습니다."));
  if (typeof document.body === "string") {
    const narrative = traceElement("div", "trace-narrative trace-part");
    const characters = Array.from(document.body); // Server offsets count Unicode code points.
    let cursor = 0;
    const items = document.items.filter((item) => item.active).sort((a, b) => a.start - b.start);
    for (const item of items) {
      const passage = traceElement("article", "trace-item");
      passage.dataset.itemId = item.id;
      passage.appendChild(traceElement("p", "trace-text", characters.slice(cursor, item.end).join("")));
      passage.appendChild(renderTraceDetails(item, subject));
      narrative.appendChild(passage);
      cursor = item.end;
    }
    if (cursor < characters.length) narrative.appendChild(traceElement("p", "trace-text", characters.slice(cursor).join("")));
    box.appendChild(narrative);
  }
  for (const part of (typeof document.body === "string" ? [] : document.parts)) {
    const items = document.items.filter((item) => item.active && item.part_id === part.id);
    if (!items.length) continue;
    const section = traceElement("section", "trace-part");
    section.dataset.partId = part.id;
    const heading = traceElement("h3", "", part.title);
    heading.appendChild(traceElement("small", "trace-part-id", part.id));
    section.appendChild(heading);
    for (const item of items) section.appendChild(renderTraceItem(item, subject));
    box.appendChild(section);
  }
  const archived = document.items.filter((item) => !item.active);
  if (archived.length) {
    const archive = traceElement("details", "trace-archive");
    archive.appendChild(traceElement("summary", "", `현재 노트에서 제외된 내용 ${archived.length}개`));
    for (const item of archived) archive.appendChild(renderTraceItem(item, subject));
    box.appendChild(archive);
  }
  if (document.legacy) {
    const legacy = traceElement("details", "trace-archive");
    legacy.appendChild(traceElement("summary", "", "ID 추적 이전 종합 노트"));
    legacy.appendChild(traceElement("p", "trace-meta", `생성 ${traceDate(document.legacy.generated_at)} · 추적 시작 ${traceDate(document.legacy.tracked_at)}. 과거 항목별 출처·추가 시각은 기록되어 있지 않습니다.`));
    legacy.appendChild(traceElement("p", "trace-text", document.legacy.text));
    box.appendChild(legacy);
  }
}

function displaySubject(subject) {
  resetNotes();
  activeSubject = subject;
  // Fresh subject/source metadata may include lectures created or moved in another tab.
  for (const note of [...subject.notes, ...(subject.source_notes || [])]) savedNotes.set(note.id, note);
  $("subject-view").hidden = false;
  $("lecture-workspace").hidden = true;
  $("note-title").textContent = subject.name;
  const count = subject.notes.filter((note) => note.has_final).length;
  $("note-meta").textContent = `과목 종합 노트 · 강의 ${subject.notes.length}개 · 최종 요약 완료 ${count}개`;
  renderSubjectSummary(subject);
  $("subject-summary").classList.toggle("placeholder", !subject.summary);
  $("subject-model").value = selectableModel(subject.summary_model) || config?.final_model || "";
  const stale = subject.summary && subject.summary_revision !== subject.revision;
  $("subject-status").textContent = stale ? "갱신 필요 · 강의 최종 노트가 변경되거나 삭제되었습니다. 아래 내용은 이전 종합 노트입니다."
    : subject.summary ? `강의 최종 노트 ${count}개 반영 · 생성 모델: ${subject.summary_model} · ${noteDate(subject.summary_updated_at)}`
    : count ? `완료된 강의 최종 노트 ${count}개를 통합할 수 있습니다.` : "완료된 강의 최종 노트가 아직 없습니다.";
  $("subject-status").dataset.stale = String(!!stale);
  const lectures = $("subject-lectures");
  lectures.replaceChildren();
  for (const note of subject.notes) lectures.appendChild(lectureButton(note));
  subjectFolders.set(subject.id, { ...subject, note_count: subject.notes.length, completed_count: count, has_summary: !!subject.summary });
  notice("");
  setState("subject");
  rememberNote(`subject:${subject.id}`);
}

async function selectSubject(id) {
  if (busyStates.has(state)) return;
  const previous = state;
  setState("loading");
  try { displaySubject(await libraryRequest(`/api/subjects/${encodeURIComponent(id)}`)); }
  catch (error) { setState(previous); notice(error.message); }
}

async function createSubject(event) {
  event.preventDefault();
  if (busyStates.has(state)) return;
  const name = $("subject-name").value.trim();
  if (!name) return;
  const previous = state;
  setState("creating");
  try {
    const subject = await libraryRequest("/api/subjects", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ name }) });
    await refreshSubjects();
    $("subject-name").value = "";
    displaySubject(subject);
  } catch (error) { setState(previous); notice(error.message); }
}

async function generateSubjectSummary() {
  if (!activeSubject || busyStates.has(state)) return;
  const id = activeSubject.id;
  const model = $("subject-model").value;
  setState("subject_summarizing");
  notice("");
  $("subject-status").textContent = "강의 최종 노트로 과목 본문을 요약한 뒤, 본문을 유지한 채 출처와 변경 이력을 붙이고 있습니다.";
  try {
    const subject = await libraryRequest(`/api/subjects/${id}/summary`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ model }), signal: AbortSignal.timeout(610000) });
    displaySubject(subject);
  } catch (error) { setState("subject"); notice(error.message); $("subject-status").textContent = "종합 노트 작성 실패 · 이전 완료 결과는 유지됩니다."; }
}

async function deleteCurrentNote() {
  if (!activeNote || busyStates.has(state)) return;
  if (!window.confirm("이 강의의 원문·실시간 요약·최종 노트를 모두 삭제할까요? 삭제 후 복구할 수 없습니다.")) return;
  const id = activeNote.id;
  const folderId = activeNote.subject_id;
  const previous = state;
  setState("deleting");
  try {
    await libraryRequest(`/api/notes/${id}`, { method: "DELETE" });
    savedNotes.delete(id);
    resetNotes();
    showLectureWorkspace();
    showNoteHeading();
    rememberNote(null);
    await refreshSubjects();
    setState("idle");
    if (folderId && subjectFolders.has(folderId)) await selectSubject(folderId);
  } catch (error) { setState(activeNote ? previous : "idle"); notice(error.message); }
}

async function moveCurrentNote() {
  if (!activeNote || busyStates.has(state)) return;
  const previous = state;
  const folderId = $("note-folder").value || null;
  setState("moving");
  try {
    const record = await libraryRequest(`/api/notes/${activeNote.id}/subject`, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ subject_id: folderId }) });
    await refreshSubjects();
    displayNote(record);
  } catch (error) { setState(previous); renderNoteFolder(); notice(error.message); }
}

async function deleteSubjectSummary() {
  if (!activeSubject || busyStates.has(state) || !window.confirm("과목 종합 노트와 항목 ID·변경 이력을 모두 삭제할까요? 강의별 노트는 유지됩니다.")) return;
  const id = activeSubject.id;
  setState("deleting");
  try { displaySubject(await libraryRequest(`/api/subjects/${id}/summary`, { method: "DELETE" })); }
  catch (error) { setState("subject"); notice(error.message); }
}

async function deleteEmptySubject() {
  if (!activeSubject || busyStates.has(state) || !window.confirm("빈 과목 폴더와 남아 있는 종합 노트를 삭제할까요?")) return;
  const id = activeSubject.id;
  setState("deleting");
  try {
    await libraryRequest(`/api/subjects/${id}`, { method: "DELETE" });
    subjectFolders.delete(id);
    activeSubject = null;
    setState("idle");
    newNote();
  } catch (error) { setState("subject"); notice(error.message); }
}

function setSubjectControls() {
  const busy = busyStates.has(state);
  $("subject-model").disabled = busy || !config;
  $("subject-generate").disabled = busy || !config?.openai_configured || !activeSubject?.notes.some((note) => note.has_final);
  $("subject-generate").textContent = activeSubject?.summary ? "종합 노트 갱신" : "종합 노트 생성";
  $("subject-new-note").disabled = busy;
  $("subject-summary-delete").hidden = !activeSubject?.summary;
  $("subject-delete").hidden = !activeSubject || !!activeSubject.notes.length;
  $("subject-summary-delete").disabled = $("subject-delete").disabled = busy;
  for (const button of $("subject-lectures").children) button.disabled = busy;
  for (const { button, available } of subjectSourceButtons) button.disabled = busy || !available;
  renderNoteFolder();
}
