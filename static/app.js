const $ = (id) => document.getElementById(id);
let config = null;
let state = "idle";
let current = null;
let transcript = "";
let startedAt = null;
const summaries = { live: "", final: "" };
const drafts = { live: "", final: "" };
const pending = { live: false, final: false };
const busyStates = new Set(["loading", "connecting", "recording", "stopping", "summarizing", "retrying", "creating", "deleting", "moving", "subject_summarizing"]);
const COURSE_STORAGE_KEY = "voice-notes.courses.v1";
let courses = [];
const liveNotes = new Map();
const SELECTED_NOTE_KEY = "voice-notes.selected-note.v1";
const savedNotes = new Map();
let activeNote = null;
let libraryWarning = "";
const MODEL_STORAGE_KEY = "voice-notes.models.v1";
let modelPreferences = null;

function readModels() {
  const models = { live_model: $("live-model").value, final_model: $("final-model").value };
  if (!config?.summary_models?.includes(models.live_model) || !config.summary_models.includes(models.final_model)) {
    throw new Error("요약 모델을 선택해 주세요.");
  }
  return models;
}

function selectableModel(model) {
  if (config?.summary_models?.includes(model)) return model;
  const migrated = config?.model_migrations?.[model];
  return config?.summary_models?.includes(migrated) ? migrated : "";
}

function fillModels(models) {
  for (const kind of ["live", "final"]) {
    const model = models?.[`${kind}_model`];
    $(`${kind}-model`).value = selectableModel(model) || modelPreferences?.[`${kind}_model`] || "";
  }
}

function changeModels() {
  if (busyStates.has(state)) return;
  try {
    modelPreferences = readModels();
    localStorage.setItem(MODEL_STORAGE_KEY, JSON.stringify(modelPreferences));
  } catch { /* Selections still apply to requests when browser storage is blocked. */ }
  setState(state);
}

function renderUsedModels() {
  const hasLive = [...liveNotes.values()].some((note) => note.text);
  $("live-used-model").textContent = hasLive ? `생성 모델: ${activeNote?.live_model || "기존 기록 · 모델 정보 없음"}` : "";
  $("final-used-model").textContent = summaries.final ? `생성 모델: ${activeNote?.final_model || "기존 기록 · 모델 정보 없음"}` : "";
}

function noteDate(value) {
  return new Date(value).toLocaleString("ko-KR", { month: "long", day: "numeric", hour: "2-digit", minute: "2-digit" });
}

function renderLibrary() {
  renderFolderLibrary();
}

function showNoteHeading() {
  $("note-title").textContent = activeNote?.course.name || "강의 노트";
  $("note-meta").textContent = activeNote
    ? `${noteDate(activeNote.createdAt)} · 원문 / 실시간 요약 / 최종 노트`
    : "한국어 설명과 영어 전문 용어를 함께 기록하고 정리하세요.";
}

function rememberNote(id) {
  try {
    if (id) localStorage.setItem(SELECTED_NOTE_KEY, id);
    else localStorage.removeItem(SELECTED_NOTE_KEY);
  } catch { /* Only the selection is local; note content lives in SQLite. */ }
}

async function selectNote(id) {
  if (busyStates.has(state) || !savedNotes.has(id)) return;
  const previousState = state;
  setState("loading");
  let record;
  try {
    const response = await fetch(`/api/notes/${encodeURIComponent(id)}`, { signal: AbortSignal.timeout(10000) });
    if (!response.ok) throw new Error("Cannot load note");
    record = await response.json();
  } catch {
    libraryWarning = "노트를 불러오지 못했습니다. 서버 연결을 확인하고 다시 선택해 주세요.";
    setState(previousState);
    return;
  }
  displayNote(record);
}

function displayNote(record) {
  resetNotes();
  activeNote = record;
  showLectureWorkspace();
  startedAt = new Date(record.createdAt);
  transcript = record.transcript;
  summaries.final = record.final;
  fillModels(record);
  $("confirmed").textContent = transcript;
  $("transcript-empty").hidden = true;
  $("char-count").textContent = `${transcript.trim().length.toLocaleString()}자 기록됨`;
  $("timer").textContent = `${String(Math.floor(record.duration / 60)).padStart(2, "0")}:${String(Math.floor(record.duration % 60)).padStart(2, "0")}`;
  for (const chunk of record.chunks) {
    startLiveNote(chunk.id, chunk.source_text);
    updateLiveNote(chunk.id, chunk.text || "요약 미완료 · 전체 최종 노트에서 확인할 수 있습니다.", chunk.status === "done" ? "done" : "failed");
  }
  renderSummary("final", record.final);
  renderUsedModels();
  libraryWarning = "";
  $("live-status").textContent = record.chunks.length ? "저장된 청크별 실시간 요약" : "저장된 실시간 요약이 없습니다.";
  $("final-status").textContent = record.final ? "저장된 최종 노트" : "최종 노트 미완료 · 저장된 원문으로 다시 시도할 수 있습니다.";
  fillCourse(record.course);
  renderCourses(courses.some((course) => course.name === record.course.name) ? record.course.name : "");
  notice("");
  showNoteHeading();
  savedNotes.set(record.id, { id: record.id, title: record.course.name || "강의 노트", createdAt: record.createdAt, has_final: !!record.final, chunk_count: record.chunks.length, subject_id: record.subject_id });
  setState(record.final ? "done" : "saved");
  rememberNote(record.id);
  for (const box of ["transcript-scroll", "live-summary", "final-summary"]) $(box).scrollTop = 0;
}

function newNote(subjectId = null) {
  if (busyStates.has(state)) return;
  resetNotes();
  showLectureWorkspace();
  const folder = subjectFolders.get(subjectId);
  if (folder) {
    fillCourse(courses.find((course) => course.name === folder.name) || { name: folder.name, context: "", terms: [] });
    renderCourses(courses.some((course) => course.name === folder.name) ? folder.name : "");
  }
  fillModels(modelPreferences);
  showNoteHeading();
  notice("");
  rememberNote(null);
  setState("idle");
}

async function loadNotes() {
  setState("loading");
  let selected = null;
  try { selected = localStorage.getItem(SELECTED_NOTE_KEY); } catch { /* SQLite works without browser storage. */ }
  try {
    const [records, folders] = await Promise.all([libraryRequest("/api/notes"), libraryRequest("/api/subjects")]);
    savedNotes.clear();
    acceptSubjects(folders);
    for (const record of records) savedNotes.set(record.id, record);
    libraryWarning = "";
  } catch { libraryWarning = "저장된 노트 목록을 불러오지 못했습니다. 서버 연결을 확인하고 새로고침해 주세요."; }
  setState("idle");
  if (selected?.startsWith("subject:") && subjectFolders.has(selected.slice(8))) return selectSubject(selected.slice(8));
  const id = savedNotes.has(selected) ? selected : savedNotes.keys().next().value;
  if (id) await selectNote(id);
  else if (subjectFolders.size) await selectSubject(subjectFolders.keys().next().value);
}

function normalizeCourse(value) {
  if (!value || typeof value.name !== "string" || typeof value.context !== "string" || !Array.isArray(value.terms)
      || value.terms.some((term) => typeof term !== "string")) throw new Error("과목 설정 형식이 올바르지 않습니다.");
  const course = { name: value.name.trim(), context: value.context.trim(), terms: [...new Set(value.terms.map((term) => term.trim()).filter(Boolean))] };
  if (course.name.length > 80 || course.context.length > 2000 || course.terms.length > 100 || course.terms.some((term) => term.length > 80)) {
    throw new Error("과목명 80자, 설명 2,000자, 용어 100개(각 80자) 이하로 입력해 주세요.");
  }
  const context = {};
  if (course.name) context.general = [{ key: "topic", value: course.name }];
  if (course.context) context.text = course.context;
  if (course.terms.length) context.terms = course.terms;
  if (new TextEncoder().encode(JSON.stringify(context)).length > 8000) throw new Error("과목 설명과 용어가 너무 깁니다. 합계 UTF-8 8,000바이트 이하로 줄여 주세요.");
  return course;
}

function readCourse() {
  return normalizeCourse({ name: $("course-name").value, context: $("course-context").value, terms: $("course-terms").value.split(/\r?\n/) });
}

function fillCourse(course = { name: "", context: "", terms: [] }) {
  $("course-name").value = course.name;
  $("course-context").value = course.context;
  $("course-terms").value = course.terms.join("\n");
}

function renderCourses(selected = "") {
  const select = $("course-select");
  select.replaceChildren();
  for (const name of ["", ...courses.map((course) => course.name)]) {
    const option = document.createElement("option");
    option.value = name;
    option.textContent = name || "새 과목 / 기본 설정";
    select.appendChild(option);
  }
  select.value = selected;
  $("course-delete").disabled = !selected;
}

function persistCourses(selected) {
  try {
    localStorage.setItem(COURSE_STORAGE_KEY, JSON.stringify({ courses, selected }));
    $("course-status").textContent = "이 브라우저에 저장했습니다. 다음 녹음부터 적용됩니다.";
  } catch {
    $("course-status").textContent = "브라우저 저장소를 사용할 수 없어 이 페이지에서만 유지됩니다. 현재 설정으로 녹음할 수 있습니다.";
  }
}

function loadCourses() {
  try {
    const data = JSON.parse(localStorage.getItem(COURSE_STORAGE_KEY) || "null");
    if (!data) return;
    if (!Array.isArray(data.courses) || data.courses.length > 50) throw new Error();
    const saved = data.courses.map(normalizeCourse);
    if (saved.some((course) => !course.name) || new Set(saved.map((course) => course.name)).size !== saved.length) throw new Error();
    courses = saved;
    const selected = courses.find((course) => course.name === data.selected);
    renderCourses(selected?.name || "");
    fillCourse(selected);
  } catch {
    $("course-status").textContent = "저장된 과목 설정을 읽을 수 없습니다. 설정을 새로 입력해 주세요.";
  }
}

function saveCourse() {
  if (busyStates.has(state)) return;
  try {
    const course = readCourse();
    if (!course.name) throw new Error("저장할 과목명을 입력해 주세요.");
    const index = courses.findIndex((saved) => saved.name === course.name);
    if (index < 0 && courses.length >= 50) throw new Error("과목은 최대 50개까지 저장할 수 있습니다.");
    if (index < 0) courses.push(course);
    else courses[index] = course;
    renderCourses(course.name);
    fillCourse(course);
    persistCourses(course.name);
  } catch (error) { $("course-status").textContent = error.message; }
}

function selectCourse() {
  if (busyStates.has(state)) return;
  const selected = $("course-select").value;
  fillCourse(courses.find((course) => course.name === selected));
  $("course-delete").disabled = !selected;
  persistCourses(selected);
}

function deleteCourse() {
  if (busyStates.has(state)) return;
  courses = courses.filter((course) => course.name !== $("course-select").value);
  renderCourses();
  fillCourse();
  persistCourses("");
}

function notice(message) {
  $("notice").textContent = message;
  $("notice").hidden = !message;
}

function setState(next) {
  state = next;
  document.body.dataset.state = next;
  const labels = {
    subject: ["과목 종합 노트", "완료된 강의 최종 노트를 통합합니다."],
    subject_summarizing: ["과목 종합 노트 작성 중", "강의별 최종 요약을 통합합니다."],
    creating: ["과목 생성 중", "과목 폴더를 저장합니다."],
    deleting: ["노트 삭제 중", "저장된 기록을 삭제합니다."],
    moving: ["과목 변경 중", "강의 노트를 다른 과목으로 옮깁니다."],
    loading: ["저장된 노트 불러오는 중", "DB에 저장된 기록을 가져옵니다."],
    idle: ["강의 녹음 준비", "한국어와 영어가 섞인 강의를 전사하고 요약합니다."],
    connecting: ["연결 준비 중", "마이크 권한을 허용해 주세요. 전사 서버에 연결합니다."],
    recording: ["강의를 기록하고 있어요", "한국어·영어 원문과 청크별 실시간 요약이 표시됩니다."],
    stopping: ["마지막 발언 정리 중", "원문이 모두 확정될 때까지 잠시 기다려 주세요."],
    summarizing: ["최종 노트 작성 중", "전체 원문에서 핵심 개념과 예시를 정리합니다."],
    retrying: ["노트 다시 작성 중", "현재 페이지의 확정된 원문으로 요청합니다."],
    done: ["기록이 완료되었어요", "결과를 다운로드하거나 새 녹음을 시작하세요."],
    saved: ["저장된 노트를 보고 있어요", "최종 노트가 미완료된 경우 저장된 원문으로 다시 시도할 수 있습니다."],
    error: ["연결을 확인해 주세요", "현재까지 확정된 원문과 완료된 노트는 유지됩니다."],
  };
  $("status").textContent = labels[next][0];
  $("status-detail").textContent = labels[next][1];
  $("start").disabled = busyStates.has(next) || !config?.soniox_configured || !config?.openai_configured;
  $("start").textContent = transcript ? "● 새 녹음" : "● 녹음 시작";
  $("stop").disabled = !["recording", "connecting"].includes(next);
  $("stop").textContent = next === "connecting" ? "연결 취소" : "■ 종료 및 요약";
  $("download").disabled = activeSubject ? !activeSubject.summary : !transcript.trim();
  $("retry").hidden = !transcript.trim() || busyStates.has(next) || (!!summaries.final && $("final-model").value === activeNote?.final_model);
  $("retry").textContent = summaries.final ? "선택한 모델로 다시 작성" : "최종 요약 다시 시도";
  $("course-settings").disabled = busyStates.has(next);
  $("live-model").disabled = $("final-model").disabled = busyStates.has(next) || !config;
  renderLibrary();
  setSubjectControls();
}

function renderSummary(kind, text) {
  const element = $(`${kind}-summary`);
  element.textContent = text || (kind === "live"
    ? "들어오는 발화 청크마다 핵심 주제와 요약을 표시합니다.\n새 내용·반복된 내용·보완·정정을 구분하고, 해당 전사 원문도 확인할 수 있습니다."
    : "강의가 끝나면 전체 내용을 한국어로 정리합니다.\n영어 전문 용어는 원문 표기를 유지합니다.");
  element.classList.toggle("placeholder", !text);
}

function liveSummaryContent(text, sourceText = "") {
  const heading = text.trimStart().match(/^주제[ \t]*[:：][ \t]*([^\r\n]+)(?:\r?\n|$)/);
  const body = heading ? text.trimStart().slice(heading[0].length).trimStart() : text;
  const firstPoint = body.split(/\r?\n/).map((line) => line.replace(/^\s*[-*#]+\s*/, "").trim())
    .find((line) => line && !["새 내용", "반복된 내용", "보완·정정", "없음", "언급 없음"].includes(line));
  const title = (heading?.[1] || firstPoint || sourceText || "요약 작성 중").replace(/\s+/g, " ").trim();
  const characters = Array.from(title);
  return { title: characters.slice(0, 80).join("") + (characters.length > 80 ? "…" : ""), body };
}

function startLiveNote(id, sourceText) {
  const box = $("live-summary");
  if (!liveNotes.size) { box.textContent = ""; box.classList.toggle("placeholder", false); }
  let note = liveNotes.get(id);
  if (!note) {
    const card = document.createElement("section");
    card.className = "live-chunk";
    const title = document.createElement("h3");
    const index = document.createElement("span");
    index.className = "live-chunk-index";
    index.textContent = `청크 ${id}`;
    const source = document.createElement("details");
    source.className = "live-source";
    const sourceLabel = document.createElement("summary");
    sourceLabel.textContent = "이 요약의 전사 원문";
    const sourceBody = document.createElement("p");
    source.appendChild(sourceLabel);
    source.appendChild(sourceBody);
    const body = document.createElement("p");
    card.appendChild(title);
    card.appendChild(index);
    card.appendChild(source);
    card.appendChild(body);
    box.appendChild(card);
    note = { card, title, source, sourceBody, body, text: "", source_text: "" };
    liveNotes.set(id, note);
  }
  if (typeof sourceText === "string") note.source_text = sourceText;
  note.sourceBody.textContent = note.source_text;
  note.source.hidden = !note.source_text;
  note.title.textContent = liveSummaryContent("", note.source_text).title;
  note.card.dataset.status = "pending";
  note.body.textContent = "요약 중…";
  return note;
}

function updateLiveNote(id, text, status) {
  const note = liveNotes.get(id);
  if (!note) return;
  const box = $("live-summary");
  const nearBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 80;
  if (status === "failed") {
    note.title.textContent = note.source_text ? liveSummaryContent("", note.source_text).title : "요약 미완료";
    note.body.textContent = text;
  } else {
    const content = liveSummaryContent(text, note.source_text);
    note.title.textContent = content.title;
    note.body.textContent = content.body;
  }
  note.card.dataset.status = status;
  if (status === "done") note.text = text;
  if (nearBottom) box.scrollTop = box.scrollHeight;
}

function cancelLiveDrafts() {
  for (const [id, note] of liveNotes) {
    if (note.card.dataset.status === "pending") {
      updateLiveNote(id, "요약 미완료 · 전체 최종 노트에서 확인할 수 있습니다.", "failed");
    }
  }
}

function resetNotes() {
  activeNote = null;
  transcript = "";
  startedAt = new Date();
  $("confirmed").textContent = "";
  $("partial").textContent = "";
  $("transcript-empty").hidden = false;
  $("char-count").textContent = "0자 기록됨";
  $("timer").textContent = "00:00";
  liveNotes.clear();
  for (const kind of ["live", "final"]) {
    summaries[kind] = drafts[kind] = "";
    pending[kind] = false;
    renderSummary(kind, "");
  }
  $("live-status").textContent = "확정 전사를 기다리는 중";
  $("final-status").textContent = "녹음 종료 후 생성";
  renderUsedModels();
}

async function loadConfig() {
  const response = await fetch("/api/config", { signal: AbortSignal.timeout(5000) });
  if (!response.ok) throw new Error("서버 설정을 읽을 수 없습니다.");
  config = await response.json();
  if (!modelPreferences) {
    modelPreferences = { live_model: config.realtime_model, final_model: config.final_model };
    try {
      const saved = JSON.parse(localStorage.getItem(MODEL_STORAGE_KEY) || "null");
      for (const key of ["live_model", "final_model"]) {
        const model = selectableModel(saved?.[key]);
        if (model) modelPreferences[key] = model;
      }
    } catch { /* Invalid or unavailable browser preferences fall back to server defaults. */ }
    for (const kind of ["live", "final"]) {
      const select = $(`${kind}-model`);
      select.replaceChildren();
      for (const model of config.summary_models) {
        const option = document.createElement("option");
        option.value = model;
        option.textContent = model.replace("gpt-", "");
        select.appendChild(option);
      }
    }
    fillModels(modelPreferences);
    initializeSubjectModels();
  }
  if (!config.soniox_configured || !config.openai_configured) {
    notice("시작 준비가 필요합니다. README 안내에 따라 서버 .env에 Soniox와 OpenAI API 키를 설정하고 서버를 다시 시작해 주세요.");
  }
  if (!liveNotes.size) renderSummary("live", summaries.live);
  setState(state);
}

async function releaseAudio(session) {
  clearInterval(session.timer);
  if (current === session && session.recording && activeNote?.id === session.noteId) {
    activeNote.duration = Math.max(0, Math.floor((Date.now() - startedAt.getTime()) / 1000));
  }
  session.recording = false;
  session.source?.disconnect();
  session.node?.disconnect();
  session.gain?.disconnect();
  session.stream?.getTracks().forEach((track) => { track.onended = null; track.stop(); });
  if (session.context && session.context.state !== "closed") await session.context.close().catch(() => {});
}

function release(session) {
  clearTimeout(session.handshake);
  session.flushResolve?.();
  void releaseAudio(session);
  session.socket?.close();
  if (current === session) current = null;
}

function fail(session, message) {
  if (current !== session) return;
  notice(message);
  for (const kind of ["live", "final"]) {
    if (pending[kind]) {
      pending[kind] = false;
      if (kind === "live") cancelLiveDrafts();
      else renderSummary(kind, summaries[kind]);
      $(`${kind}-status`).textContent = "연결 중단 · 완료된 내용 유지";
    }
  }
  $("partial").textContent = "";
  release(session);
  setState("error");
}

function appendText(scrollId, confirmedId, partialId, emptyId, fullText, delta, partial) {
  const box = $(scrollId);
  const nearBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 80;
  if (!$(confirmedId).firstChild) $(confirmedId).appendChild(document.createTextNode(""));
  $(confirmedId).firstChild.appendData(delta);
  $(partialId).textContent = partial;
  $(emptyId).hidden = !!(fullText || partial);
  if (nearBottom) box.scrollTop = box.scrollHeight;
}

function receive(session, event) {
  if (current !== session) return;
  if (event.type === "ready") {
    clearTimeout(session.handshake);
    resetNotes();
    showLectureWorkspace();
    activeNote = { id: event.note_id, createdAt: event.created_at, course: session.course, ...session.models,
      live_model: event.live_model || session.models?.live_model, final_model: event.final_model || session.models?.final_model };
    fillModels(activeNote);
    session.noteId = activeNote.id;
    showNoteHeading();
    session.recording = true;
    session.source.connect(session.node);
    session.node.connect(session.gain);
    session.gain.connect(session.context.destination);
    session.timer = setInterval(() => {
      const seconds = Math.floor((Date.now() - startedAt.getTime()) / 1000);
      $("timer").textContent = `${String(Math.floor(seconds / 60)).padStart(2, "0")}:${String(seconds % 60).padStart(2, "0")}`;
      if (seconds >= config.max_session_seconds && state === "recording") void stopRecording();
    }, 500);
    setState("recording");
  } else if (event.type === "transcript") {
    transcript += event.delta;
    appendText("transcript-scroll", "confirmed", "partial", "transcript-empty", transcript, event.delta, event.partial);
    $("char-count").textContent = `${transcript.trim().length.toLocaleString()}자 기록됨`;
    $("download").disabled = !transcript.trim();
  } else if (event.type === "stopping") {
    void releaseAudio(session);
    setState("stopping");
    if (event.message) notice(event.message);
  } else if (event.type === "transcript_done") {
    $("partial").textContent = "";
    cancelLiveDrafts();
    pending.live = false;
    $("live-status").textContent = "완료된 실시간 요약 유지 · 남은 내용은 최종 노트에 포함";
    setState("summarizing");
  } else if (event.type === "summary_start") {
    pending[event.kind] = true;
    drafts[event.kind] = "";
    if (event.kind === "live") startLiveNote(event.chunk_id, event.source_text);
    $(`${event.kind}-status`).textContent = event.kind === "live" ? `${event.chunk_id}번째 청크 요약 중…` : "요약 작성 중…";
  } else if (event.type === "summary_delta") {
    drafts[event.kind] += event.delta;
    if (event.kind === "live") updateLiveNote(event.chunk_id, drafts.live, "pending");
    else renderSummary(event.kind, drafts[event.kind]);
  } else if (event.type === "summary_done") {
    if (event.kind === "live") updateLiveNote(event.chunk_id, event.text, "done");
    else summaries[event.kind] = event.text;
    pending[event.kind] = false;
    if (event.kind !== "live") renderSummary(event.kind, event.text);
    if (event.model && activeNote) activeNote[`${event.kind}_model`] = event.model;
    renderUsedModels();
    $(`${event.kind}-status`).textContent = `${new Date().toLocaleTimeString("ko-KR", { hour: "2-digit", minute: "2-digit" })} 업데이트`;
  } else if (event.type === "error") {
    if (event.fatal) return fail(session, event.message);
    notice(event.message);
    if (event.kind) {
      pending[event.kind] = false;
      if (event.kind === "live") {
        updateLiveNote(event.chunk_id, event.retrying ? "요약 실패 · 다시 시도합니다." : "요약 실패 · 전체 최종 노트에서 확인할 수 있습니다.", "failed");
      } else renderSummary(event.kind, summaries[event.kind]);
      $(`${event.kind}-status`).textContent = event.kind === "live" ? (event.retrying ? "요약 실패 · 잠시 후 재시도" : "청크 요약 실패 · 최종 노트에서 확인") : "요약 실패 · 다시 시도 가능";
    }
  } else if (event.type === "complete") {
    if (event.message) notice(event.message);
    release(session);
    setState(event.final_ok ? "done" : (transcript.trim() ? "error" : "idle"));
  } else if (event.type === "note_saved") {
    const previous = savedNotes.get(event.note.id);
    // Transcript checkpoints do not change the list; avoid rebuilding it every second.
    if (!libraryWarning && previous && ["createdAt", "title", "has_final", "chunk_count", "subject_id"].every((key) => previous[key] === event.note[key])) return;
    savedNotes.set(event.note.id, event.note);
    if (activeNote?.id === event.note.id) activeNote.subject_id = event.note.subject_id;
    libraryWarning = "";
    rememberNote(event.note.id);
    renderLibrary();
    renderNoteFolder();
    if ((event.note.subject_id && !subjectFolders.has(event.note.subject_id)) || previous?.has_final !== event.note.has_final) {
      void refreshSubjects().catch((error) => { libraryWarning = error.message; renderLibrary(); });
    }
  } else if (event.type === "storage_error") {
    libraryWarning = event.message;
    renderLibrary();
  }
}

async function startRecording() {
  if (busyStates.has(state)) return;
  notice("");
  let course;
  let models;
  try { course = readCourse(); models = readModels(); }
  catch (error) { notice(error.message); return; }
  const session = { course, models };
  current = session;
  setState("connecting");
  try {
    if (!navigator.mediaDevices?.getUserMedia || !window.AudioWorkletNode) {
      throw new Error("마이크 녹음은 최신 Chrome/Edge의 localhost 또는 HTTPS에서 사용할 수 있습니다.");
    }
    session.stream = await navigator.mediaDevices.getUserMedia({ audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true }, video: false });
    if (current !== session) return release(session);
    session.context = new AudioContext({ sampleRate: 16000 });
    await session.context.resume();
    await session.context.audioWorklet.addModule("/static/pcm-worklet.js");
    if (current !== session) return release(session);
    session.source = session.context.createMediaStreamSource(session.stream);
    session.node = new AudioWorkletNode(session.context, "pcm-recorder", { channelCount: 1, channelCountMode: "explicit" });
    session.gain = session.context.createGain();
    session.gain.gain.value = 0;
    session.node.port.onmessage = ({ data }) => {
      if (current !== session) return;
      if (data?.type === "stopped") return session.flushResolve?.();
      if (data instanceof ArrayBuffer && session.recording && session.socket.readyState === WebSocket.OPEN) {
        if (session.socket.bufferedAmount > 1024 * 1024) {
          return fail(session, "네트워크가 오디오 전송 속도를 따라가지 못합니다. 현재 전사를 저장한 뒤 다시 시작해 주세요.");
        }
        session.socket.send(data);
      }
    };
    session.socket = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws/session`);
    session.socket.onopen = () => {
      if (current === session) session.socket.send(JSON.stringify({ type: "start", sample_rate: session.context.sampleRate, course: session.course, ...session.models }));
    };
    session.socket.onmessage = ({ data }) => {
      try { receive(session, JSON.parse(data)); }
      catch { fail(session, "서버 응답을 처리할 수 없습니다. 현재 전사를 저장해 주세요."); }
    };
    session.socket.onerror = () => fail(session, "서버 연결에 실패했습니다. FastAPI 서버가 실행 중인지 확인해 주세요.");
    session.socket.onclose = () => fail(session, "연결이 끊겼습니다. 현재 전사를 저장하거나 최종 요약을 다시 시도할 수 있습니다.");
    session.handshake = setTimeout(() => fail(session, "전사 서버 연결 시간이 초과되었습니다. Soniox 설정과 네트워크를 확인해 주세요."), 25000);
    session.stream.getAudioTracks()[0].onended = () => { if (state === "recording") void stopRecording(); };
  } catch (error) {
    const message = error.name === "NotAllowedError" ? "마이크 권한이 필요합니다. 브라우저 사이트 설정에서 마이크를 허용해 주세요."
      : error.name === "NotFoundError" ? "사용 가능한 마이크를 찾을 수 없습니다." : error.message;
    fail(session, message);
  }
}

async function stopRecording() {
  const session = current;
  if (!session) return;
  if (state === "connecting") { release(session); setState("idle"); return; }
  if (state !== "recording") return;
  setState("stopping");
  try {
    // The worklet sends its last PCM buffer before acknowledging stop.
    await new Promise((resolve, reject) => {
      const timeout = setTimeout(() => reject(new Error("마지막 오디오를 전송하지 못했습니다. 현재 전사를 저장해 주세요.")), 1500);
      session.flushResolve = () => { clearTimeout(timeout); resolve(); };
      session.node.port.postMessage("stop");
    });
    if (current !== session) return;
    if (session.socket.readyState !== WebSocket.OPEN) throw new Error("녹음 종료 중 서버 연결이 끊겼습니다.");
    session.socket.send(JSON.stringify({ type: "stop" }));
    await releaseAudio(session);
  } catch (error) { fail(session, error.message); }
}

async function retrySummary() {
  if (busyStates.has(state) || !transcript.trim()) return;
  let selectedModel;
  try { selectedModel = readModels().final_model; }
  catch (error) { notice(error.message); return; }
  notice("");
  setState("retrying");
  $("final-status").textContent = "요약 재요청 중…";
  try {
    const snapshot = activeNote?.id ? {
      note_id: activeNote.id, course: activeNote.course, created_at: activeNote.createdAt,
      chunks: [...liveNotes].map(([id, note]) => ({ id, text: note.text, status: note.text ? "done" : "incomplete", ...(note.source_text ? { source_text: note.source_text } : {}) })),
      duration: activeNote.duration || 0,
      ...(activeNote.live_model ? { live_model: activeNote.live_model } : {}),
    } : {};
    const response = await fetch("/api/summary/final", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ transcript, ...snapshot, model: selectedModel }), signal: AbortSignal.timeout(125000),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(typeof data.detail === "string" ? data.detail : "요약 요청이 실패했습니다.");
    if (data.note) {
      displayNote(data.note);
      void refreshSubjects().catch((error) => { libraryWarning = error.message; renderLibrary(); });
      return;
    }
    summaries.final = data.text;
    if (activeNote) activeNote.final_model = data.model || selectedModel;
    renderSummary("final", data.text);
    renderUsedModels();
    $("final-status").textContent = "최종 요약 완료";
    setState("done");
    if (activeNote?.id) {
      savedNotes.set(activeNote.id, { id: activeNote.id, title: activeNote.course.name || "강의 노트", createdAt: activeNote.createdAt, has_final: true, chunk_count: liveNotes.size });
      libraryWarning = "";
      rememberNote(activeNote.id);
    }
    renderLibrary();
  } catch (error) {
    notice(error.message);
    $("final-status").textContent = "요약 실패 · 다시 시도 가능";
    setState("error");
  }
}

function downloadNotes() {
  if (activeSubject) {
    const stale = activeSubject.summary_revision !== activeSubject.revision ? "갱신 필요: 강의 노트가 변경된 후의 이전 종합 노트입니다.\n\n" : "";
    return downloadText(`# ${activeSubject.name} · 과목 종합 노트\n\n${stale}생성 모델: ${activeSubject.summary_model}\n\n${activeSubject.summary_export || activeSubject.summary}\n`, `subject-notes-${activeSubject.id}.md`);
  }
  const liveText = [...liveNotes].filter(([, note]) => note.text).map(([id, note]) => {
    const content = liveSummaryContent(note.text, note.source_text);
    const source = note.source_text ? `\n\n전사 원문:\n${note.source_text}` : "";
    return `### ${content.title}\n\n청크 ${id}\n\n${content.body}${source}`;
  }).join("\n\n");
  const text = `# ${activeNote?.course.name || "강의 노트"}\n\n${startedAt?.toLocaleString("ko-KR") || ""}\n\n다운로드 시점에 확정된 원문과 생성이 완료된 노트만 포함합니다.\n\n## 최종 노트\n\n${summaries.final || "완료된 최종 노트가 없습니다."}\n\n## 실시간 요약 노트\n\n${liveText || "완료된 실시간 요약이 없습니다."}\n\n## 원문 (한국어·영어)\n\n${transcript.trim()}\n`;
  downloadText(text, `lecture-notes-${(startedAt || new Date()).toISOString().replace(/[:.]/g, "-")}.md`);
}

function downloadText(text, filename) {
  const url = URL.createObjectURL(new Blob([text], { type: "text/markdown;charset=utf-8" }));
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

$("start").addEventListener("click", startRecording);
$("stop").addEventListener("click", stopRecording);
$("retry").addEventListener("click", retrySummary);
$("download").addEventListener("click", downloadNotes);
$("new-note").addEventListener("click", () => newNote());
$("subject-create-form").addEventListener("submit", createSubject);
$("subject-generate").addEventListener("click", generateSubjectSummary);
$("subject-new-note").addEventListener("click", () => newNote(activeSubject?.id));
$("note-delete").addEventListener("click", deleteCurrentNote);
$("note-folder").addEventListener("change", moveCurrentNote);
$("subject-summary-delete").addEventListener("click", deleteSubjectSummary);
$("subject-delete").addEventListener("click", deleteEmptySubject);
$("course-save").addEventListener("click", saveCourse);
$("course-select").addEventListener("change", selectCourse);
$("course-delete").addEventListener("click", deleteCourse);
$("live-model").addEventListener("change", changeModels);
$("final-model").addEventListener("change", changeModels);
window.addEventListener("beforeunload", (event) => {
  if (busyStates.has(state)) { event.preventDefault(); event.returnValue = ""; }
});
async function initialize() {
  setState("loading");
  loadCourses();
  await loadConfig().catch((error) => notice(error.message));
  await loadNotes();
}
const initialized = initialize();
