import {ClipBuffer, clipDirectory, savedClips} from "./clips.js?v=3";
import {FaceView} from "./face-view.js";
const $ = id => document.getElementById(id);
const encoder = new TextEncoder();
let stream, audio, socket, capture;
let running = false;
let recording, stopping, clipURL, selection = 0;
const files = new Map();
const directory = clipDirectory();
const alternateHost = location.hostname === "localhost" ? "127.0.0.1" : "localhost";
$("other-origin").href = location.protocol + "//" + alternateHost + ":" + location.port + "/";
$("other-origin").textContent = alternateHost + "에서 저장한 클립 보기";
let previewURL;
function clearEarly() {
  const early = $("early");
  early.hidden = true;
  if (previewURL) {
    early.pause();
    early.removeAttribute("src");
    early.load();
    URL.revokeObjectURL(previewURL);
    previewURL = null;
  }
}
const signal = (message, state = "") => {
  $("signal").textContent = message;
  $("signal").dataset.state = state;
};
const reactionName = type => ({laughter:"웃음", scream:"비명", gasp:"놀람"}[type] || type);

async function showClip(autoplay = false) {
  const token = ++selection;
  const clip = files.get($("clips").value);
  if (!clip) return;
  const file = await clip.handle.getFile();
  if (token !== selection) return;
  if (clipURL) URL.revokeObjectURL(clipURL);
  clipURL = URL.createObjectURL(file);
  $("player").src = clipURL;
  $("download").href = clipURL;
  $("download").download = file.name;
  $("playback").textContent = "";
  if (autoplay) {
    $("playback").textContent = "저장된 클립 재생 준비 중";
    // A background tab can defer playback until visible. Saving must finish independently.
    $("player").play().then(() => {
      if (token === selection) $("playback").textContent = "방금 저장한 클립 재생 중";
    }).catch(() => {
      if (token === selection) $("playback").textContent = "플레이어의 ▶ 버튼을 눌러 재생하세요.";
    });
  }
}
function addClip(clip, prepend) {
  if (files.has(clip.handle.name)) return;
  files.set(clip.handle.name, clip);
  $("saved-count").textContent = "(" + files.size + ")";
  const timestamp = new Date(clip.time).toLocaleTimeString("ko-KR");
  const option = new Option(timestamp + " · " + reactionName(clip.reaction) + " · " + clip.id, clip.handle.name);
  if (prepend) $("clips").prepend(option);
  else $("clips").append(option);
  $("empty").hidden = true;
  $("saved").hidden = false;
}
$("clips").onchange = () => showClip().catch(err => report(err.message));
$("player").onpause = () => { $("playback").textContent = ""; };
$("player").onended = () => { $("playback").textContent = "클립 재생 완료"; };
directory.then(savedClips).then(async clips => {
  for (const clip of clips) addClip(clip, false);
  if (clips.length) await showClip();
}).catch(err => signal("녹화 저장소를 열 수 없습니다 · " + err.message));

const report = text => { $("log").textContent = text + "\n" + $("log").textContent.slice(0, 4000); };

function stop() {
  if (!stopping) stopping = endSession().finally(() => { stopping = null; });
  return stopping;
}
async function endSession() {
  running = false;
  $("stop").disabled = true;
  if (capture) capture.port.onmessage = null;
  if (socket) socket.onmessage = null;
  const pending = recording?.stop();
  recording = null;
  stream?.getTracks().forEach(track => track.stop());
  socket?.close(1000);
  if (audio && audio.state !== "closed") await audio.close();
  await pending;
  if ($("signal").dataset.state === "waiting") signal("시연 종료 · 수신한 녹화 신호 없음");
  $("preview").srcObject = null;
  $("start").disabled = false;
}

$("stop").onclick = stop;
$("start").onclick = async () => {
  $("start").disabled = true;
  $("log").textContent = "";
  signal("탭 공유 준비 중");
  $("peaks").textContent = "";
  $("rule").textContent = "";
  clearEarly();
  $("clip-progress").textContent = "";
  $("faces").textContent = "얼굴 검출 결과 대기";
  $("scores").textContent = "웃음소리 — · 미소 —";
  let seq = 0, chunk = 0, windows = 0, peakLaughter = 0, peakSmile = null;
  const faceView = new FaceView();
  const session = crypto.randomUUID();
  const base = {schema:"encore.v1", session_id:session};
  try {
    stream = await navigator.mediaDevices.getDisplayMedia({video:true, audio:true});
    if (!stream.getAudioTracks().length) throw Error("탭 오디오 공유를 켜주세요.");
    $("preview").srcObject = stream;
    await $("preview").play();
    audio = new AudioContext({sampleRate:16000});
    await audio.resume();
    await audio.audioWorklet.addModule("pcm.js?v=3");
    const ws = socket = new WebSocket("ws://127.0.0.1:8765/v1/sessions/" + session + "/stream");
    await new Promise((resolve,reject) => {
      ws.onopen = resolve;
      ws.onerror = () => reject(Error("서버 또는 SSH 터널 연결 실패"));
    });
    ws.onclose = () => {
      if (socket === ws && running) {
        report("서버 연결 종료");
        signal("서버 연결이 끊겼습니다 · 시연을 다시 시작하세요", "error");
        stop();
      }
    };
    recording = new ClipBuffer(stream, await directory, {
      onSignal(phase, e) {
        const label = reactionName(e.reaction.type) + " · " + e.event_id;
        if (phase === "OPEN") {
          signal("● 녹화 신호 수신 · 녹화 중 · " + label, "recording");
          if ($("early").hidden) $("clip-progress").textContent = "녹화 중 · 첫 장면을 준비하고 있습니다.";
        }
        if (phase === "UPDATE") signal("● 반응 지속 · 녹화 연장 · " + label, "recording");
        if (phase === "FINAL") signal("반응 종료 · 클립 마무리 중 · " + label, "recording");
      },
      onPreview(blob, id) {
        clearEarly();
        previewURL = URL.createObjectURL(blob);
        $("early").src = previewURL;
        $("early").hidden = false;
        $("clip-progress").textContent = "녹화 중 · 첫 장면 미리보기 · " + id;
        $("early").play().catch(() => {});
      },
      onPreviewError(err) {
        report("미리보기 오류 · " + err.message);
        $("clip-progress").textContent = "전체 클립 녹화 중 · 미리보기를 열 수 없습니다.";
      },
      async onSaved(clip) {
        addClip(clip, true);
        report("클립 저장 완료 · " + clip.id);
        if (!recording?.active) {
          clearEarly();
          $("clip-progress").textContent = "";
          signal("클립 저장 완료 · " + clip.id, "saved");
        }
        if ($("player").paused) {
          $("clips").value = clip.handle.name;
          await showClip(true).catch(err => report("클립 열기 실패 · " + err.message));
        }
      },
      onError(err) {
        report("녹화 오류 · " + err.message);
        signal("녹화 오류 · " + err.message, "error");
        if (running) void stop();
      }
    });
    ws.onmessage = ({data}) => {
      const msg = JSON.parse(data);
      if (msg.kind === "demo_scores") {
        windows++;
        if (typeof msg.face_detected === "boolean") {
          faceView.observe(msg.face_frames, msg.face_detected);
          const face = msg.face_detected ? "얼굴 검출" : "얼굴 미검출";
          $("faces").textContent = `최근 프레임 · ${face} (누적 ${msg.faces_seen}/${msg.face_frames}장)`;
        } else {
          $("faces").textContent = msg.smile == null ? "이 구간에 일치하는 얼굴 점수 없음" : "이 구간에서 얼굴 검출";
        }
        peakLaughter = Math.max(peakLaughter, msg.laughter);
        if (msg.smile != null) peakSmile = Math.max(peakSmile ?? 0, msg.smile);
        $("scores").textContent = `분석 ${windows}회 · 웃음소리 ${msg.laughter.toFixed(2)} · 미소 ${msg.smile?.toFixed(2) ?? "얼굴 점수 없음"}`;
        $("peaks").textContent = `세션 최고값 · 웃음소리 ${peakLaughter.toFixed(2)} · 미소 ${peakSmile?.toFixed(2) ?? "미검출"}`;
        if (msg.assisted_audio_threshold != null) {
          const hold = msg.quiet_elapsed_ms > 0
            ? ` · 웃음 잦아듦 ${(msg.quiet_elapsed_ms/1000).toFixed(1)}초` : "";
          $("rule").textContent = `웃음 시작 · 소리 ≥ ${msg.laughter_threshold} 또는 소리 ≥ ${msg.assisted_audio_threshold} + 미소 ≥ ${msg.smile_threshold} · 종료 대기 ${msg.quiet_ms/1000}초${hold}`;
        } else {
          $("rule").textContent = "이전 서버 설정으로 연결됨 · 새 녹화 기준을 적용하려면 시연 서버를 재시작하세요.";
        }
      } else if (msg.kind === "event_update") {
        const e = msg.event;
        recording?.receive(e);
        report(`PC 수신 · ${e.phase} · ${e.event_id} · ${e.reaction.type} · 오디오 신뢰도 ${e.reaction.confidence} · 표정 강도 ${e.intensity.visual ?? "—"}`);
      } else if (msg.kind === "control_ack") report(msg.sensor + " · " + msg.status);
    };
    const epoch = performance.now() - audio.currentTime*1000;
    const t0 = Math.round(performance.now());
    for (const sensor of ["microphone","camera"]) {
      ws.send(JSON.stringify({...base, kind:"control_update", seq:seq++, capture_ms:t0,
        idempotency_key:sensor+"-1", sensor, enabled:true, generation:1}));
    }
    const canvas = $("face-input");
    const ctx = canvas.getContext("2d",{willReadFrequently:true});
    capture = new AudioWorkletNode(audio,"encore-capture",{channelCount:1,channelCountMode:"explicit"});
    capture.port.onmessage = ({data:{pcm,startMs}}) => {
      const index = chunk++;
      if (ws.readyState !== WebSocket.OPEN || ws.bufferedAmount > 524288) return;
      const imageMs = Math.round(epoch + audio.currentTime*1000);
      const area = faceView.draw(ctx, $("preview"));
      $("roi").hidden = false;
      $("roi-name").textContent = "서버에 보낸 화면 · " + area;
      const rgba = ctx.getImageData(0,0,320,180).data;
      const header = encoder.encode(JSON.stringify({...base, kind:"audio_chunk", seq:seq++,
        capture_ms:Math.round(epoch+startMs), generation:1, chunk:index, sample_rate:16000,
        channels:1, encoding:"pcm_s16le", samples:pcm.byteLength/2, aec:"off"}));
      const tail = 16 + 320*180*3;
      const packet = new Uint8Array(tail+4+header.length+pcm.byteLength);
      packet.set(encoder.encode("EMV1"));
      const view = new DataView(packet.buffer);
      view.setUint32(4,1);
      view.setBigUint64(8,BigInt(imageMs));
      for (let i=0,j=16;i<rgba.length;i+=4) {
        packet[j++]=rgba[i]; packet[j++]=rgba[i+1]; packet[j++]=rgba[i+2];
      }
      view.setUint32(tail,header.length);
      packet.set(header,tail+4);
      packet.set(new Uint8Array(pcm),tail+4+header.length);
      ws.send(packet);
    };
    const source = audio.createMediaStreamSource(new MediaStream(stream.getAudioTracks()));
    source.connect(capture);
    capture.connect(audio.destination); // processor output is silence
    running = true;
    stream.getVideoTracks()[0].onended = stop;
    $("stop").disabled = false;
    signal("탭 영상 전송 중 · 서버 녹화 신호 대기", "waiting");
    report("서버 연결 · 탭 영상과 소리 전송 중");
  } catch (err) {
    report(err.message);
    signal("시연 시작 실패 · " + err.message, "error");
    await stop();
  }
};
