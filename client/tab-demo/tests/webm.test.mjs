// Local codec integration test. No camera, microphone, network or user clips.
import assert from "node:assert/strict";
import {spawnSync} from "node:child_process";
import {mkdtemp, readFile, rm, stat, writeFile} from "node:fs/promises";
import {tmpdir} from "node:os";
import {join} from "node:path";
import test from "node:test";
import {assemble} from "../clips/webm.js";

const hasGst = spawnSync("gst-launch-1.0", ["--version"]).status === 0;
function gst(pipeline) {
  const proc = spawnSync("gst-launch-1.0", ["-q", ...pipeline], {encoding:"utf8"});
  assert.equal(proc.status, 0, proc.stderr);
}

test("VP8/Opus from two independently encoded segments decodes across the join", {skip:!hasGst}, async () => {
  const dir = await mkdtemp(join(tmpdir(), "encore-webm-"));
  try {
    const parts = [];
    for (const [i, pattern] of ["ball","black"].entries()) {
      const path = join(dir, pattern+".webm");
      gst(["webmmux","name=mux","streamable=true","!","filesink","location="+path,
        "videotestsrc","pattern="+pattern,"num-buffers=12","!","video/x-raw,framerate=12/1,width=96,height=64",
        "!","vp8enc","deadline=1","!","queue","!","mux.",
        "audiotestsrc","num-buffers=50","samplesperbuffer=960","!","audio/x-raw,rate=48000",
        "!","opusenc","!","queue","!","mux."]);
      parts.push({start:i*1000,end:(i+1)*1000,file:new Blob([await readFile(path)])});
    }
    const result = join(dir,"clip.webm"), pixels = join(dir,"video.rgba"), sound = join(dir,"mono.s16");
    await writeFile(result, new Uint8Array(await (await assemble(parts)).arrayBuffer()));
    gst(["filesrc","location="+result,"!","matroskademux","name=d",
      "d.video_0","!","queue","!","vp8dec","!","videoconvert","!","video/x-raw,format=RGBA",
      "!","filesink","location="+pixels,
      "d.audio_0","!","queue","!","opusdec","!","audio/x-raw,format=S16LE,channels=1",
      "!","filesink","location="+sound]);
    assert.equal((await stat(pixels)).size, 24*96*64*4);
    assert.ok(Math.abs((await stat(sound)).size / 2 - 96000) < 960);
  } finally {
    await rm(dir, {recursive:true,force:true});
  }
});
