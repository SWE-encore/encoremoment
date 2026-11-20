import assert from "node:assert/strict";
import test from "node:test";
import {ClipPlan, missing, overlaps} from "../clips/plan.js";
import {ClipBuffer} from "../clips/buffer.js";

const event = (revision, triggers_ms, phase="UPDATE") => ({revision, phase, event_id:"evt-1",
  capture:{triggers_ms}, generations:{camera:1}, reaction:{type:"laughter"}});

test("replayed revisions and triggers extend exactly once", () => {
  const plan = new ClipPlan("evt-1", "laughter");
  plan.apply(event(1,[20000], "OPEN"));
  const first = plan.parts[0];
  assert.deepEqual([first.start, first.end], [5000,30000]);
  plan.apply(event(3,[20000,30000]));
  plan.apply(event(2,[20000,22000]));
  plan.apply(event(4,[20000,30000],"FINAL"));
  assert.equal(first.end,35000);
  assert.ok(first.sealed);
  assert.equal(plan.apply(event(5,[20000,33000])), false);
});

test("the next trigger after the cap or after expiry starts a linked interval", () => {
  for (const times of [[20000,31000], [20000,22000,24000,26000,28000,30000,32000,34000,36000]]) {
    const plan = new ClipPlan("evt-1","laughter");
    plan.apply(event(4,times,"FINAL"));
    assert.equal(plan.parts.length,2);
    assert.equal(plan.parts[0].sealed,true);
    if (times.length > 2) assert.equal(plan.parts[0].end-plan.parts[0].start,60000);
    assert.equal(plan.parts[1].index,2);
    assert.equal(plan.parts[1].start,times.at(-1)-15000);
  }
});

test("records activation and interior gaps and retains editing handles", () => {
  assert.deepEqual(missing(-10000,15000,[{start:0,end:5000},{start:8000,end:16000}]),
    [[-10000,0],[5000,8000]]);
  assert.ok(overlaps({start:1000,end:5000},{start:5000,end:30000}));
  assert.ok(!overlaps({start:35000,end:40000},{start:5000,end:30000}));
});

function ring() {
  // The media encoder is tested separately with real files; here drive capture time.
  const result = Object.create(ClipBuffer.prototype);
  const removed = [], saved = [];
  Object.assign(result, {plans:new Map(), generation:1, queue:Promise.resolve(), segments:[],
    dir:{removeEntry:async name=>removed.push(name)}, onSignal(){}, onError:err=>{throw err;},
    current:{finish:async()=>{}}, save:async(plan,part)=>saved.push([plan.id,part.end])});
  for (let i=0;i<10;i++) result.segments.push({start:i*5000,end:(i+1)*5000,handle:{name:"s"+i}});
  return {result,removed,saved};
}

test("OPEN pins pre-context during eviction; FINAL waits for future footage", async () => {
  const {result,removed,saved} = ring();
  result.receive(event(1,[20000],"OPEN"));
  await result.queue;
  assert.ok(!removed.includes("s0"));
  result.segments = result.segments.filter(s=>s.end<=30000);
  result.receive(event(3,[20000,24000],"FINAL"));
  await result.queue;
  assert.deepEqual(saved,[]);
  await result.update(39999);
  assert.deepEqual(saved,[]);
  await result.update(40000);
  await result.update(45000);
  assert.deepEqual(saved,[["evt-1",35000]]);
  assert.ok(removed.includes("s0"));
});

test("generation mismatch, duplicate FINAL, and stop cannot resurrect a job", async () => {
  const {result,saved,removed} = ring();
  result.receive({...event(1,[20000],"OPEN"),generations:{camera:2}});
  assert.equal(result.plans.size,0);
  result.receive(event(1,[20000],"OPEN"));
  result.receive(event(2,[20000],"FINAL"));
  await result.stop();
  result.receive(event(2,[20000],"FINAL"));
  assert.deepEqual(saved,[]);
  assert.equal(removed.length,10);
});
