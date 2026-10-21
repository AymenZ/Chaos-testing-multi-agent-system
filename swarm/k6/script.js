// swarm/k6/script.js
// One fixed script for every run. All variation comes from /work/config.json, written by runner.py,
// so agents never write load-test code. Requests are tagged with the phase they were sent in
// (warmup, ramp, hold); only the "hold" phase is measured.
import http from 'k6/http';
import exec from 'k6/execution';

const C = JSON.parse(open('/work/config.json'));

// Anything outside 200-399 (including timeouts) counts as failed, so abort_error_rate can stop a run early.
http.setResponseCallback(http.expectedStatuses({ min: 200, max: 399 }));

// k6 only reports a tagged sub-metric if a threshold mentions it. These all pass by construction;
// they exist to make k6 report per-phase and per-status numbers. The one real threshold is the abort.
const thresholds = {
  http_req_failed: [{ threshold: `rate<${C.abort_error_rate}`, abortOnFail: true, delayAbortEval: '10s' }],
  'http_req_duration{phase:hold}': ['p(95)>=0'],
  'http_reqs{phase:hold}': ['count>=0'],
};
for (const s of C.statuses) {
  thresholds[`http_reqs{phase:hold,status:${s}}`] = ['count>=0'];
  thresholds[`http_reqs{status:${s}}`] = ['count>=0'];
}

// k6 moves the rate LINEARLY from the previous stage's target to each stage's own target. So the hold
// stage is only flat if the stage before it already ended at the target rate: there is always a ramp
// stage (at least 1 s), otherwise "hold" would glide up from the warmup rate across the whole window.
const RAMP_S = Math.max(C.ramp_s, 1);
const stages = [];
if (C.warmup_s > 0) stages.push({ duration: `${C.warmup_s}s`, target: C.warmup_rps });
stages.push({ duration: `${RAMP_S}s`, target: C.target_rps });
stages.push({ duration: `${C.hold_s}s`, target: C.target_rps });

export const options = {
  summaryTrendStats: ['med', 'p(95)', 'p(99)', 'max'],
  scenarios: {
    load: {
      executor: 'ramping-arrival-rate',
      startRate: C.warmup_s > 0 ? C.warmup_rps : C.target_rps,
      timeUnit: '1s',
      preAllocatedVUs: C.pre_vus,
      maxVUs: C.max_vus,
      gracefulStop: `${C.timeout_s}s`,
      stages,
    },
  },
  thresholds,
};

function phase() {
  const t = (Date.now() - exec.scenario.startTime) / 1000;
  if (t < C.warmup_s) return 'warmup';
  if (t < C.warmup_s + RAMP_S) return 'ramp';
  return 'hold';
}

export function setup() {
  if (!C.login) return {};
  const asJson = C.login.encoding === 'json';
  const res = http.post(C.login.url, asJson ? JSON.stringify(C.login.body) : C.login.body, {
    headers: asJson ? { 'Content-Type': 'application/json' } : {},
    tags: { phase: 'setup' },
  });
  const token = res.status === 200 ? res.json(C.login.token_field) : null;
  if (!token) exec.test.abort(`login failed with status ${res.status}`);
  return { token };
}

export default function (data) {
  const params = { headers: {}, tags: { phase: phase() }, timeout: `${C.timeout_s}s` };
  let body = null;
  if (C.json_body !== null) {
    body = JSON.stringify(C.json_body);
    params.headers['Content-Type'] = 'application/json';
  } else if (C.form_body !== null) {
    body = C.form_body;
  }
  if (data && data.token) params.headers['Authorization'] = `Bearer ${data.token}`;
  http.request(C.method, C.url, body, params);
}

export function handleSummary(data) {
  return { '/work/result.json': JSON.stringify(data) };
}
