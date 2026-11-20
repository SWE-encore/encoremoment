// Local codec integration test. No camera, microphone, network or user clips.
import assert from "node:assert/strict";
import {spawnSync} from "node:child_process";
import {mkdtemp, readFile, rm, stat, writeFile} from "node:fs/promises";
import {tmpdir} from "node:os";
import {join} from "node:path";
import test from "node:test";
import {assembleClips, downloadableWebm} from "../clips/webm.js";

const hasGst = spawnSync("gst-launch-1.0", ["--version"]).status === 0;
function gst(pipeline) {
  const proc = spawnSync("gst-launch-1.0", ["-q", ...pipeline], {encoding:"utf8"});
  assert.equal(proc.status, 0, proc.stderr);
}

async function capturePart(dir, i, width, audioFirst = false) {
  const path = join(dir, "input-" + i + ".webm");
  const video = ["videotestsrc","pattern=ball","num-buffers=12","!",
    "video/x-raw,framerate=12/1,width="+width+",height=64","!","vp8enc","deadline=1","!","queue","!","mux."];
  const sound = ["audiotestsrc","num-buffers=50","samplesperbuffer=960","!","audio/x-raw,rate=48000",
    "!","opusenc","!","queue","!","mux."];
  gst(["webmmux","name=mux","streamable=true","!","filesink","location="+path,
    ...(audioFirst ? sound.concat(video) : video.concat(sound))]);
  return {start:i*1000, end:(i+1)*1000, file:new Blob([await readFile(path)])};
}

async function verifyDecoded(dir, clip, width, seconds) {
  const result = join(dir,"clip.webm"), pixels = join(dir,"video.rgba"), sound = join(dir,"mono.s16");
  await writeFile(result, new Uint8Array(await clip.blob.arrayBuffer()));
  gst(["filesrc","location="+result,"!","matroskademux","name=d",
    "d.video_0","!","queue","!","vp8dec","!","videoconvert","!","video/x-raw,format=RGBA",
    "!","filesink","location="+pixels,
    "d.audio_0","!","queue","!","opusdec","!","audio/x-raw,format=S16LE,channels=1",
    "!","filesink","location="+sound]);
  assert.equal((await stat(pixels)).size, seconds*12*width*64*4);
  assert.ok(Math.abs((await stat(sound)).size / 2 - seconds*48000) < 960);
}

test("VP8 and Opus decode across recorder track renumbering and every resolution boundary", {skip:!hasGst}, async () => {
  const dir = await mkdtemp(join(tmpdir(), "encore-webm-"));
  try {
    const parts = [
      await capturePart(dir, 0, 96), await capturePart(dir, 1, 96, true),
      await capturePart(dir, 2, 128), await capturePart(dir, 3, 128), await capturePart(dir, 4, 96)
    ];
    const clips = await assembleClips(parts);
    assert.deepEqual(clips.map(c => [c.start, c.end]), [[0,2000], [2000,4000], [4000,5000]]);
    await verifyDecoded(dir, clips[0], 96, 2);
    await verifyDecoded(dir, clips[1], 128, 2);
    await verifyDecoded(dir, clips[2], 96, 1);
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
