// Local codec integration test. No camera, microphone, network or user clips.
import assert from "node:assert/strict";
import {spawnSync} from "node:child_process";
import {mkdtemp, readFile, rm, stat, writeFile} from "node:fs/promises";
import {tmpdir} from "node:os";
import {join} from "node:path";
import test from "node:test";
import {assemble, downloadableWebm} from "../clips/webm.js";

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

function chromiumAlphaSample(withAdditional) {
  // Structural fixture only. No captured audio or video.
  const element = (id, ...fields) => {
    const body = Buffer.concat(fields);
    return Buffer.concat([Buffer.from(id, "hex"), Buffer.from([0x80 + body.length]), body]);
  };
  const track = element("ae", element("d7", Buffer.from([1])), element("86", Buffer.from("V_VP8")),
    element("e0", element("53c0", Buffer.from([1]))));
  const block = Buffer.from([0x81,0,0,0x80,0]);
  const sample = withAdditional
    ? element("a0", element("a1", block), element("75a1", element("a6", element("a5", Buffer.from([1])))))
    : element("a3", block);
  return new Blob([element("18538067", element("1654ae6b", track), element("1f43b675", sample))],
    {type:"video/webm"});
}

test("opaque Chromium download changes only the unsupported alpha declaration", async () => {
  const input = chromiumAlphaSample(false);
  const original = Buffer.from(await input.arrayBuffer());
  const expected = Buffer.from(original);
  const flag = expected.indexOf(Buffer.from("53c08101", "hex"));
  assert.ok(flag >= 0);
  expected[flag+3] = 0;
  const download = await downloadableWebm(input);
  assert.deepEqual(Buffer.from(await download.arrayBuffer()), expected);
  assert.deepEqual(Buffer.from(await input.arrayBuffer()), original);
  assert.equal(await downloadableWebm(download), download);
});

test("download preserves the original track when a block carries additional data", async () => {
  const input = chromiumAlphaSample(true);
  assert.equal(await downloadableWebm(input), input);
});
