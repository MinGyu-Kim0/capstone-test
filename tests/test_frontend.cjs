// Check stream rendering, retry input, export, and reset without a browser or microphone.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

async function main() {
  function element() {
    return {
      children: [], dataset: {}, hidden: false, value: "", scrollHeight: 100, scrollTop: 0, clientHeight: 100,
      classList: { toggle() {} }, addEventListener() {}, click() {}, setAttribute(name, value) { this[name] = value; },
      get firstChild() { return this.children[0] || null; },
      get textContent() { return this.children.map((child) => child.textContent ?? child.data).join(""); },
      set textContent(value) { this.children = value ? [{ data: String(value) }] : []; },
      appendChild(node) { this.children.push(node); return node; },
      replaceChildren(...nodes) { this.children = nodes; },
    };
  }
  const nodes = new Map([...fs.readFileSync("static/index.html", "utf8").matchAll(/id="([^"]+)"/g)].map(([, id]) => [id, element()]));
  let exported;
  let retryBody;
  const stored = new Map();
  const records = new Map();
  let detailFailure = false;
  let detailWait = null;
  const noteId = "00000000-0000-4000-8000-000000000001";
  let storageFailure = false;
  let microphoneRequests = 0;
  const config = { soniox_configured: true, openai_configured: true, summary_trigger: "endpoint", previous_chunks: 2, max_session_seconds: 7200,
    summary_models: ["gpt-6-sol", "gpt-6-luna"], realtime_model: "gpt-6-luna", final_model: "gpt-6-sol",
    model_migrations: { "gpt-5.6-sol": "gpt-6-sol", "gpt-5.6-terra": "gpt-6-sol", "gpt-5.6-luna": "gpt-6-luna" } };
  const mediaNode = () => ({ connect() {}, disconnect() {} });
  class TestAudioContext {
    constructor() { this.sampleRate = 16000; this.state = "running"; this.audioWorklet = { async addModule() {} }; }
    async resume() {}
    async close() { this.state = "closed"; }
    createMediaStreamSource() { return mediaNode(); }
    createGain() { return { ...mediaNode(), gain: {} }; }
  }
  class TestWorklet { constructor() { Object.assign(this, mediaNode()); this.port = {}; } }
  class TestSocket {
    static OPEN = 1;
    constructor() { this.sent = []; this.readyState = 1; }
    send(value) { this.sent.push(JSON.parse(value)); }
    close() {}
  }
  const context = vm.createContext({
    document: {
      body: { dataset: {} },
      getElementById(id) { assert.ok(nodes.has(id), `Missing HTML element: ${id}`); return nodes.get(id); },
      createTextNode(data) { return { data, appendData(value) { this.data += value; } }; },
      createElement: element,
    },
    window: { addEventListener() {}, AudioWorkletNode: TestWorklet },
    AudioContext: TestAudioContext, AudioWorkletNode: TestWorklet, WebSocket: TestSocket,
    location: { protocol: "http:", host: "localhost" },
    navigator: { mediaDevices: { async getUserMedia() {
      microphoneRequests++;
      const track = { stop() {} };
      return { getTracks: () => [track], getAudioTracks: () => [track] };
    } } },
    localStorage: {
      getItem(key) { if (storageFailure) throw new Error("storage blocked"); return stored.get(key) || null; },
      setItem(key, value) { if (storageFailure) throw new Error("storage blocked"); stored.set(key, value); },
      removeItem(key) { stored.delete(key); },
    },
    setInterval() {}, clearInterval() {}, setTimeout() {}, clearTimeout() {},
    AbortSignal: { timeout() {} }, Blob, TextEncoder,
    URL: { createObjectURL(blob) { exported = blob; return "blob:test"; }, revokeObjectURL() {} },
    fetch: async (url, options) => {
      if (url === "/api/config") return { ok: true, json: async () => config };
      if (url === "/api/subjects") return { ok: true, json: async () => [] };
      if (url === "/api/notes") return { ok: true, json: async () => [...records.values()].map((note) => ({ id: note.id, createdAt: note.createdAt, title: note.course.name, has_final: !!note.final, chunk_count: note.chunks.length })) };
      if (url.startsWith("/api/notes/")) {
        if (detailWait) await detailWait;
        if (detailFailure) throw new Error("offline");
        return { ok: true, json: async () => records.get(url.split("/").pop()) };
      }
      assert.equal(url, "/api/summary/final");
      retryBody = JSON.parse(options.body);
      if (retryBody.note_id) {
        records.get(retryBody.note_id).final = "강의 개요\n- acceleration은 속도의 변화율입니다.";
        records.get(retryBody.note_id).final_model = retryBody.model;
      }
      return { ok: true, json: async () => ({ text: "강의 개요\n- acceleration은 속도의 변화율입니다.", model: retryBody.model, note: records.get(retryBody.note_id) }) };
    },
    testSession: {
      models: { live_model: "gpt-6-sol", final_model: "gpt-6-luna" },
      course: { name: "일반물리학", context: "뉴턴의 운동 법칙", terms: ["momentum", "각운동량"] },
      source: { connect() {}, disconnect() {} }, node: { connect() {}, disconnect() {} },
      gain: { connect() {}, disconnect() {} }, context: { destination: {}, state: "closed" },
      socket: { close() {} },
    },
  });
  vm.runInContext(fs.readFileSync("static/subjects.js", "utf8"), context);
  vm.runInContext(fs.readFileSync("static/app.js", "utf8"), context);
  await vm.runInContext("initialized", context);
  assert.deepEqual(nodes.get("live-model").children.map((option) => option.value), config.summary_models);
  assert.deepEqual(nodes.get("final-model").children.map((option) => option.value), config.summary_models);
  assert.deepEqual(nodes.get("subject-model").children.map((option) => option.value), config.summary_models);
  assert.equal(nodes.get("live-model").value, "gpt-6-luna");
  assert.equal(nodes.get("final-model").value, "gpt-6-sol");
  for (const [saved, selected] of [...Object.entries(config.model_migrations), ["gpt-6-luna", "gpt-6-luna"], ["unsupported", null]]) {
    stored.set("voice-notes.models.v1", JSON.stringify({ live_model: saved, final_model: saved }));
    await vm.runInContext("modelPreferences = null; loadConfig()", context);
    assert.equal(nodes.get("live-model").value, selected || config.realtime_model);
    assert.equal(nodes.get("final-model").value, selected || config.final_model);
  }
  nodes.get("live-model").value = "gpt-6-sol";
  nodes.get("final-model").value = "gpt-6-luna";
  vm.runInContext("changeModels()", context);
  assert.deepEqual(JSON.parse(stored.get("voice-notes.models.v1")), { live_model: "gpt-6-sol", final_model: "gpt-6-luna" });
  nodes.get("course-name").value = " 일반물리학 ";
  nodes.get("course-context").value = " 뉴턴의 운동 법칙 ";
  nodes.get("course-terms").value = " momentum \n각운동량\nmomentum\n";
  vm.runInContext("saveCourse()", context);
  const expectedCourse = { name: "일반물리학", context: "뉴턴의 운동 법칙", terms: ["momentum", "각운동량"] };
  assert.deepEqual(JSON.parse(stored.get("voice-notes.courses.v1")).courses, [expectedCourse]);
  nodes.get("course-name").value = "화학";
  nodes.get("course-context").value = "산과 염기";
  nodes.get("course-terms").value = "pH";
  vm.runInContext("saveCourse()", context);
  nodes.get("course-select").value = "일반물리학";
  vm.runInContext("selectCourse()", context);
  assert.equal(nodes.get("course-terms").value, "momentum\n각운동량");
  vm.runInContext("fillCourse(); loadCourses()", context);
  assert.equal(nodes.get("course-name").value, "일반물리학");
  await vm.runInContext("startRecording()", context);
  assert.equal(nodes.get("course-settings").disabled, true);
  vm.runInContext("current.socket.onopen()", context);
  assert.deepEqual(JSON.parse(vm.runInContext("JSON.stringify(current.socket.sent[0])", context)), { type: "start", sample_rate: 16000, course: expectedCourse, live_model: "gpt-6-sol", final_model: "gpt-6-luna" });
  assert.equal(nodes.get("live-model").disabled, true);
  assert.equal(nodes.get("final-model").disabled, true);
  const snapshot = stored.get("voice-notes.courses.v1");
  vm.runInContext("saveCourse(); deleteCourse()", context);
  assert.equal(stored.get("voice-notes.courses.v1"), snapshot);
  await vm.runInContext("stopRecording()", context);
  assert.equal(nodes.get("course-settings").disabled, false);
  assert.equal(microphoneRequests, 1);
  nodes.get("course-context").value = "x".repeat(2001);
  await vm.runInContext("startRecording()", context);
  assert.equal(microphoneRequests, 1, "invalid settings must not open the microphone");
  vm.runInContext("loadCourses()", context);
  storageFailure = true;
  vm.runInContext("saveCourse()", context);
  assert.ok(nodes.get("course-status").textContent.includes("이 페이지에서만"));
  storageFailure = false;
  stored.set("voice-notes.courses.v1", "{broken");
  vm.runInContext("loadCourses()", context);
  assert.ok(nodes.get("course-status").textContent.includes("읽을 수 없습니다"));
  vm.runInContext("saveCourse()", context);
  nodes.get("course-select").value = "화학";
  vm.runInContext("selectCourse(); deleteCourse()", context);
  assert.equal(JSON.parse(stored.get("voice-notes.courses.v1")).courses.length, 1);
  vm.runInContext("current = testSession", context);
  const emit = (event) => { context.testEvent = event; vm.runInContext("receive(testSession, testEvent)", context); };
  emit({ type: "ready", note_id: noteId, created_at: "2026-09-23T01:00:00Z" });
  emit({ type: "transcript", delta: "acceleration은 ", partial: "미확정 내용" });
  emit({ type: "transcript", delta: "속도의 변화율입니다.", partial: "" });
  assert.equal(nodes.get("confirmed").textContent, "acceleration은 속도의 변화율입니다.");
  assert.equal(nodes.get("partial").textContent, "");
  const firstSource = "acceleration은 속도의 변화율입니다.";
  const firstSummary = "주제: acceleration의 정의\n새 내용\n- acceleration의 정의\n반복된 내용\n- 없음\n보완·정정\n- 없음";
  emit({ type: "summary_start", kind: "live", chunk_id: 1, source_text: firstSource });
  assert.equal(vm.runInContext("liveNotes.get(1).sourceBody.textContent", context), firstSource);
  emit({ type: "summary_delta", kind: "live", chunk_id: 1, delta: "미완성 구간" });
  emit({ type: "error", kind: "live", chunk_id: 1, retrying: true, message: "retry" });
  emit({ type: "summary_start", kind: "live", chunk_id: 1 });
  emit({ type: "summary_delta", kind: "live", chunk_id: 1, delta: "주제: acceleration의 " });
  emit({ type: "summary_delta", kind: "live", chunk_id: 1, delta: "정의\n새 내용\n- acceleration의 정의" });
  emit({ type: "summary_done", kind: "live", chunk_id: 1, text: firstSummary });
  assert.equal(vm.runInContext("liveNotes.get(1).title.textContent", context), "acceleration의 정의");
  assert.equal(vm.runInContext("liveNotes.get(1).sourceBody.textContent", context), firstSource, "retry keeps the same source");
  assert.equal(vm.runInContext("liveNotes.get(1).source.hidden", context), false);
  for (const heading of ["새 내용", "반복된 내용", "보완·정정"]) {
    assert.ok(vm.runInContext("liveNotes.get(1).body.textContent", context).includes(heading));
  }
  assert.ok(!vm.runInContext("liveNotes.get(1).body.textContent", context).includes("주제:"), "the topic renders once as a heading");
  emit({ type: "summary_start", kind: "live", chunk_id: 2 });
  emit({ type: "summary_done", kind: "live", chunk_id: 2, text: "반복된 내용\n- 정의의 반복" });
  assert.equal(nodes.get("live-summary").children.length, 2, "retry reuses its chunk card");
  assert.ok(nodes.get("live-summary").textContent.includes("acceleration의 정의"));
  emit({ type: "summary_start", kind: "live", chunk_id: 3 });
  emit({ type: "summary_delta", kind: "live", chunk_id: 3, delta: "미완성 구간" });
  emit({ type: "transcript_done" });
  assert.ok(!nodes.get("live-summary").textContent.includes("미완성 구간"));
  emit({ type: "summary_start", kind: "final" });
  emit({ type: "summary_delta", kind: "final", delta: "미완성 노트" });
  emit({ type: "error", kind: "final", fatal: false, message: "재시도" });
  emit({ type: "complete", final_ok: false });
  assert.equal(nodes.get("retry").hidden, false);
  records.set(noteId, { id: noteId, createdAt: "2026-09-23T01:00:00Z", course: expectedCourse,
    transcript: "acceleration은 속도의 변화율입니다.", final: "", duration: 125,
    chunks: [{ id: 1, text: firstSummary, status: "done", source_text: firstSource }, { id: 2, text: "반복된 내용\n- 정의의 반복", status: "done" }, { id: 3, text: "", status: "incomplete" }] });
  await vm.runInContext("loadNotes()", context);
  await vm.runInContext("retrySummary()", context);
  assert.equal(retryBody.transcript, "acceleration은 속도의 변화율입니다.");
  assert.equal(retryBody.note_id, noteId);
  assert.equal(retryBody.model, "gpt-6-luna");
  assert.deepEqual(retryBody.chunks, records.get(noteId).chunks);
  assert.equal(vm.runInContext("liveNotes.get(1).sourceBody.textContent", context), firstSource, "restored notes keep their chunk source");
  assert.equal(vm.runInContext("liveNotes.get(2).source.hidden", context), true, "old notes do not guess chunk boundaries");
  vm.runInContext("downloadNotes()", context);
  const markdown = await exported.text();
  assert.ok(markdown.includes("acceleration은 속도의 변화율입니다."));
  assert.ok(markdown.includes("강의 개요"));
  assert.ok(markdown.includes("## 실시간 요약 노트"));
  assert.ok(markdown.includes("### acceleration의 정의") && markdown.includes("### 정의의 반복"));
  assert.ok(markdown.includes("청크 1") && markdown.includes("청크 2"));
  assert.ok(markdown.includes(`전사 원문:\n${firstSource}`));
  assert.ok(markdown.includes("acceleration의 정의") && markdown.includes("정의의 반복"));
  assert.ok(!markdown.includes("청크 3") && !markdown.includes("미완성 구간"));
  assert.ok(!markdown.includes("미확정 내용") && !markdown.includes("미완성 노트"));
  assert.equal(nodes.get("retry").hidden, true);
  nodes.get("final-model").value = "gpt-6-sol";
  vm.runInContext("changeModels()", context);
  assert.equal(nodes.get("retry").hidden, false, "a completed note can be regenerated with a different model");
  assert.ok(nodes.get("final-used-model").textContent.includes("gpt-6-luna"), "selection does not relabel the existing result");
  await vm.runInContext("retrySummary()", context);
  assert.equal(retryBody.model, "gpt-6-sol");
  assert.ok(nodes.get("final-used-model").textContent.includes("gpt-6-sol"));
  assert.equal(nodes.get("retry").hidden, true);
  const secondId = "00000000-0000-4000-8000-000000000002";
  records.set(secondId, { id: secondId, createdAt: "2026-09-23T02:00:00Z", course: { name: "화학", context: "산과 염기", terms: ["pH"] }, transcript: "산성 pH", final: "화학 최종 노트", chunks: [{ id: 1, text: "화학 실시간 요약", status: "done" }], duration: 60 });
  await vm.runInContext("loadNotes()", context);
  context.secondId = secondId;
  await vm.runInContext("selectNote(secondId)", context);
  assert.equal(nodes.get("confirmed").textContent, "산성 pH");
  assert.ok(nodes.get("live-summary").textContent.includes("화학 실시간 요약"));
  assert.equal(nodes.get("final-summary").textContent, "화학 최종 노트");
  assert.equal(nodes.get("note-title").textContent, "화학");
  assert.equal(nodes.get("note-count").textContent, "2");
  context.firstId = noteId;
  detailFailure = true;
  await vm.runInContext("selectNote(firstId)", context);
  assert.equal(nodes.get("confirmed").textContent, "산성 pH", "failed load preserves current note");
  assert.ok(nodes.get("library-status").textContent.includes("불러오지 못했습니다"));
  detailFailure = false;
  let releaseDetail;
  detailWait = new Promise((resolve) => { releaseDetail = resolve; });
  const loading = vm.runInContext("selectNote(firstId)", context);
  const beforeRequests = microphoneRequests;
  await vm.runInContext("newNote(); startRecording(); selectNote(secondId)", context);
  assert.equal(microphoneRequests, beforeRequests, "loading locks new recordings and note navigation");
  assert.equal(nodes.get("new-note").disabled, true);
  releaseDetail();
  await loading;
  detailWait = null;
  assert.equal(nodes.get("timer").textContent, "02:05");
  assert.ok(nodes.get("final-summary").textContent.includes("강의 개요"));
  assert.ok(nodes.get("live-summary").textContent.includes("정의의 반복"));
  await vm.runInContext("loadConfig()", context);
  assert.ok(nodes.get("live-summary").textContent.includes("정의의 반복"), "config refresh must not erase restored chunks");
  storageFailure = true;
  await vm.runInContext("loadNotes()", context);
  assert.equal(nodes.get("note-count").textContent, "2", "SQLite notes load without localStorage");
  storageFailure = false;
  vm.runInContext("newNote()", context);
  assert.equal(nodes.get("confirmed").textContent, "");
  assert.equal(vm.runInContext("liveNotes.size", context), 0);
  assert.equal(nodes.get("note-count").textContent, "2", "new note preserves library");
  assert.equal(nodes.get("final-model").value, "gpt-6-sol", "new recordings use remembered preferences");
  records.get(secondId).live_model = "gpt-5.6-sol";
  records.get(secondId).final_model = "gpt-5.6-terra";
  await vm.runInContext("selectNote(secondId)", context);
  assert.equal(nodes.get("live-model").value, "gpt-6-sol");
  assert.equal(nodes.get("final-model").value, "gpt-6-sol");
  assert.ok(nodes.get("live-used-model").textContent.includes("gpt-5.6-sol"));
  assert.ok(nodes.get("final-used-model").textContent.includes("gpt-5.6-terra"));
  await vm.runInContext("retrySummary()", context);
  assert.equal(retryBody.model, "gpt-6-sol");
  assert.equal(retryBody.live_model, "gpt-5.6-sol", "regeneration sends historical live metadata without relabeling it");
  assert.ok(nodes.get("final-used-model").textContent.includes("gpt-6-sol"));
  assert.ok(nodes.get("live-used-model").textContent.includes("gpt-5.6-sol"));
  const source = { note_id: secondId, title: "화학", created_at: "2026-09-23T02:00:00Z", quote: "화학 최종 노트", final_hash: "a".repeat(64) };
  const sourceNote = { id: secondId, title: "화학", createdAt: source.created_at, has_final: true, chunk_count: 1, subject_id: "subject", final_hash: source.final_hash };
  const item = { id: "N0001", part_id: "P0001", text: "화학 내용", active: true, sources: [source], created_at: "2026-09-23T03:00:00Z", updated_at: "2026-09-23T03:00:00Z", history: [{ id: "U0001", kind: "added", at: "2026-09-23T03:00:00Z", part_id: "P0001", part_title: "개념", text: "화학 내용", sources: [source], model: "gpt-5.6-luna" }] };
  context.traceSubject = { id: "subject", name: "과목 추적", summary: "추적 노트", summary_model: "gpt-5.6-luna", summary_updated_at: item.created_at, revision: 1, summary_revision: 1,
    notes: [sourceNote], source_notes: [sourceNote], summary_document: { version: 1, parts: [{ id: "P0001", title: "개념" }], items: [item] } };
  vm.runInContext("savedNotes.delete(secondId); displaySubject(traceSubject)", context);
  assert.equal(nodes.get("subject-model").value, "gpt-6-luna");
  assert.ok(nodes.get("subject-status").textContent.includes("gpt-5.6-luna"));
  assert.equal(item.history[0].model, "gpt-5.6-luna");
  assert.equal(vm.runInContext("savedNotes.has(secondId)", context), true, "fresh subject metadata repairs a stale tab's lecture cache");
  assert.equal(vm.runInContext("subjectSourceButtons[0].button.disabled", context), false);
  assert.ok(nodes.get("subject-summary").textContent.includes("N0001"));
  assert.ok(nodes.get("subject-summary").textContent.includes(source.quote));
  vm.runInContext("setState('subject_summarizing')", context);
  assert.equal(vm.runInContext("subjectSourceButtons[0].button.disabled", context), true, "source navigation locks during generation");
  vm.runInContext("setState('subject')", context);
  assert.equal(vm.runInContext("subjectSourceButtons[0].button.disabled", context), false);
  context.traceSubject.notes = [];
  context.traceSubject.source_notes = [{ ...sourceNote, subject_id: "elsewhere" }];
  vm.runInContext("displaySubject(traceSubject)", context);
  assert.ok(nodes.get("subject-summary").textContent.includes("다른 과목으로 이동됨"));
  assert.equal(vm.runInContext("subjectSourceButtons[0].button.disabled", context), false);
  context.traceSubject.source_notes = [];
  vm.runInContext("displaySubject(traceSubject)", context);
  assert.ok(nodes.get("subject-summary").textContent.includes("삭제된 강의"), "fresh source snapshot overrides stale cached records");
  assert.equal(vm.runInContext("subjectSourceButtons[0].button.disabled", context), true);
  const unicodeBody = "𝑥\n\n다음 문단이다.";
  const unicodeItems = [
    { ...item, text: "𝑥", start: 0, end: 1 },
    { ...item, id: "N0002", text: "다음 문단이다.", start: 3, end: 11 },
  ];
  context.traceSubject.summary_document = { version: 2, body: unicodeBody, parts: [{ id: "P0001", title: "본문" }], items: unicodeItems };
  vm.runInContext("displaySubject(traceSubject)", context);
  const narrative = nodes.get("subject-summary").children.find((node) => node.className === "trace-narrative trace-part");
  assert.equal(narrative.children.map((passage) => passage.firstChild.textContent).join(""), unicodeBody, "annotation offsets preserve whole Unicode code points and every body character");
  console.log("PASS: courses, streams, export, archive, retries, loading locks, new notes, trace rendering, cross-tab source metadata, moved/deleted citations");
}

main().catch((error) => { console.error(error); process.exitCode = 1; });
