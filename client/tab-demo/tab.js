import {ClipBuffer} from "./clips/buffer.js";
import {clipDirectory, savedClips} from "./clips/store.js";
import {downloadableWebm} from "./clips/webm.js";
import {FaceView} from "./sensing/face-view.js";
const $ = id => document.getElementById(id);
const encoder = new TextEncoder();
let stream, audio, socket, capture;
let running = false;
let recording, stopping, clipURL, selection = 0;
const files = new Map();
const directory = clipDirectory();
const alternateHost = location.hostname === "localhost" ? "127.0.0.1" : "localhost";
$("other-origin").href = location.protocol + "//" + alternateHost + ":" + location.port + "/";
$("other-origin").textContent = "View clips saved on " + alternateHost;
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
const reactionName = type => ({laughter:"Laughter", scream:"Scream", gasp:"Gasp"}[type] || type);

// Browser exception messages follow the browser locale. The names are stable across locales.
function errorText(err) {
  if (!(err instanceof DOMException)) return err.message;
  const messages = {
    NotAllowedError: "Permission was denied. Check the browser permissions and try again.",
    NotReadableError: "The browser could not read the selected source.",
    NotFoundError: "The requested source or file is unavailable.",
    NotSupportedError: "This browser does not support the requested operation.",
    QuotaExceededError: "There is not enough storage space to save the clip.",
    AbortError: "The browser canceled the operation."
  };
  return messages[err.name] || `Browser error (${err.name})`;
}

async function showClip(autoplay = false) {
  const token = ++selection;
  const clip = files.get($("clips").value);
  if (!clip) return;
  $("download").removeAttribute("href");
  const file = await clip.handle.getFile();
  const media = await downloadableWebm(file);
  if (token !== selection) return;
  if (clipURL) URL.revokeObjectURL(clipURL);
  clipURL = URL.createObjectURL(media);
  const m = clip.meta;
  const start = m ? Math.max(0, (m.start_ms - m.media_start_ms) / 1000) : 0;
  const end = m ? Math.min(m.end_ms - m.media_start_ms, m.media_end_ms - m.media_start_ms) / 1000 : 0;
  $("player").src = end > start ? clipURL + "#t=" + start + "," + end : clipURL;
  $("download").href = clipURL;
  $("download").download = file.name;
  $("playback").textContent = "";
  if (autoplay) {
    $("playback").textContent = "Preparing the saved clip for playback";
    // A background tab can defer playback until visible. Saving must finish independently.
    $("player").play().then(() => {
      if (token === selection) $("playback").textContent = "Playing the new clip";
    }).catch(() => {
      if (token === selection) $("playback").textContent = "Press Play to watch the clip.";
    });
  }
}
function addClip(clip, prepend) {
  if (files.has(clip.handle.name)) return;
  files.set(clip.handle.name, clip);
  $("saved-count").textContent = "(" + files.size + ")";
  const timestamp = new Date(clip.time).toLocaleTimeString("en-US");
  const option = new Option(timestamp + " · " + reactionName(clip.reaction) + " · " + clip.id + (clip.meta?.status === "partial" ? " · Partial clip" : ""), clip.handle.name);
  if (prepend) $("clips").prepend(option);
  else $("clips").append(option);
  $("empty").hidden = true;
  $("saved").hidden = false;
}
$("clips").onchange = () => showClip().catch(err => report(errorText(err)));
$("player").onpause = () => { $("playback").textContent = ""; };
$("player").onended = () => { $("playback").textContent = "Playback finished"; };
directory.then(savedClips).then(async clips => {
  for (const clip of clips) addClip(clip, false);
  if (clips.length) await showClip();
}).catch(err => signal("Could not open clip storage · " + errorText(err)));

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
  clearEarly();
  stream?.getTracks().forEach(track => track.stop());
  socket?.close(1000);
  if (audio && audio.state !== "closed") await audio.close();
  try { await pending; } catch (err) {
    report("Could not delete temporary files · " + errorText(err));
    signal("Storage error · " + errorText(err), "error");
  }
  if ($("signal").dataset.state === "recording") signal("Session ended. Unsaved footage was discarded.");
  if ($("signal").dataset.state === "waiting") signal("Session ended without a recording signal.");
  $("preview").srcObject = null;
  $("start").disabled = false;
}

$("stop").onclick = stop;
$("start").onclick = async () => {
  $("start").disabled = true;
  $("log").textContent = "";
  signal("Preparing to share a tab");
  $("peaks").textContent = "";
  $("rule").textContent = "";
  clearEarly();
  $("clip-progress").textContent = "";
  $("faces").textContent = "Waiting for face detection";
  $("scores").textContent = "Laughter — · Smile —";
  let seq = 0, chunk = 0, windows = 0, peakLaughter = 0, peakSmile = null;
  const faceView = new FaceView();
  const session = crypto.randomUUID();
  const base = {schema:"encore.v1", session_id:session};
  try {
    stream = await navigator.mediaDevices.getDisplayMedia({video:true, audio:true});
    if (!stream.getAudioTracks().length) throw Error("Select the tab again and turn on tab audio sharing.");
    $("preview").srcObject = stream;
    await $("preview").play();
    audio = new AudioContext({sampleRate:16000});
    await audio.resume();
    await audio.audioWorklet.addModule(new URL("./sensing/pcm.js", import.meta.url));
    const ws = socket = new WebSocket("ws://127.0.0.1:8765/v1/sessions/" + session + "/stream");
    await new Promise((resolve,reject) => {
      ws.onopen = resolve;
      ws.onerror = () => reject(Error("Could not connect. Check the server and SSH tunnel."));
    });
    ws.onclose = () => {
      if (socket === ws && running) {
        report("Server connection closed");
        signal("Lost connection to the server. Start a new session.", "error");
        stop();
      }
    };
    recording = new ClipBuffer(stream, await directory, {
      onSignal(phase, e) {
        const label = reactionName(e.reaction.type) + " · " + e.event_id;
        if (phase === "OPEN") {
          signal("Recording signal received · Recording · " + label, "recording");
          if ($("early").hidden) $("clip-progress").textContent = "Recording · Preparing a preview";
        }
        if (phase === "UPDATE") signal("Reaction continues · Extending recording · " + label, "recording");
        if (phase === "UNAVAILABLE") signal("No footage available to save · " + label);
        if (phase === "FINAL") signal("Reaction ended · Finishing clip · " + label, "recording");
      },
      onPreview(blob, id) {
        clearEarly();
        previewURL = URL.createObjectURL(blob);
        $("early").src = previewURL;
        $("early").hidden = false;
        $("clip-progress").textContent = "Recording · Preview of the first frames · " + id;
        $("early").play().catch(() => {});
      },
      onPreviewError(err) {
        report("Preview error · " + errorText(err));
        $("clip-progress").textContent = "Preview unavailable. The full clip is still recording.";
      },
      async onSaved(clip) {
        addClip(clip, true);
        report("Clip saved · " + clip.id);
        if (!recording?.active) {
          clearEarly();
          $("clip-progress").textContent = "";
          signal("Clip saved · " + clip.id, "saved");
        }
        if ($("player").paused && (clip.meta.file_index ?? 1) === 1) {
          $("clips").value = clip.handle.name;
          await showClip(true).catch(err => report("Could not open clip · " + errorText(err)));
        }
      },
      onError(err) {
        report("Recording error · " + errorText(err));
        signal("Recording error · " + errorText(err), "error");
        if (running) void stop();
      }
    });
    ws.onmessage = ({data}) => {
      const msg = JSON.parse(data);
      if (msg.kind === "demo_scores") {
        windows++;
        if (typeof msg.face_detected === "boolean") {
          faceView.observe(msg.face_chunk == null ? msg.face_frames : msg.face_chunk + 1, msg.face_detected);
          const face = msg.face_detected ? "Face detected" : "No face detected";
          $("faces").textContent = `Latest frame · ${face} (${msg.faces_seen}/${msg.face_frames} frames with a face this session)`;
        } else {
          $("faces").textContent = msg.smile == null ? "No face score for this window" : "Face detected in this window";
        }
        peakLaughter = Math.max(peakLaughter, msg.laughter);
        if (msg.smile != null) peakSmile = Math.max(peakSmile ?? 0, msg.smile);
        $("scores").textContent = `Window ${windows} · Laughter ${msg.laughter.toFixed(2)} · Smile ${msg.smile?.toFixed(2) ?? "unavailable"}`;
        $("peaks").textContent = `Session peaks · Laughter ${peakLaughter.toFixed(2)} · Smile ${peakSmile?.toFixed(2) ?? "unavailable"}`;
        if (msg.target_seconds != null) {
          const hold = msg.quiet_elapsed_ms > 0
            ? ` · Quiet for ${(msg.quiet_elapsed_ms/1000).toFixed(1)} s` : "";
          $("rule").textContent = `Laughter trigger · Audio ≥ ${msg.laughter_threshold} or combined audio and smile score over ${msg.fusion_window_ms/1000} s: ${msg.evidence_seconds.toFixed(2)} of ${msg.target_seconds.toFixed(2)} score-seconds · Inference time: audio ${msg.audio_infer_ms} ms / face ${msg.face_infer_ms ?? "—"} ms · End after ${msg.quiet_ms/1000} s of quiet${hold}`;
        } else {
          $("rule").textContent = "Connected to an older server configuration. Restart the server to use the new recording thresholds.";
        }
      } else if (msg.kind === "event_update") {
        const e = msg.event;
        recording?.receive(e);
        if (e.phase === "OPEN") report("First trigger latency (capture to receipt): " + Math.round(performance.now() - e.capture.triggers_ms[0]) + " ms");
        report(`Event received · ${e.phase} · ${e.event_id} · ${reactionName(e.reaction.type)} · Audio confidence ${e.reaction.confidence} · Facial expression intensity ${e.intensity.visual ?? "—"}`);
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
      const area = faceView.draw(ctx, $("preview"), index+1);
      $("roi").hidden = false;
      $("roi-name").textContent = "Frame sent to server · " + area;
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
    signal("Streaming tab · Waiting for a recording signal", "waiting");
    report("Connected to server · Streaming tab video and audio");
  } catch (err) {
    report(errorText(err));
    signal("Could not start session · " + errorText(err), "error");
    await stop();
  }
};
