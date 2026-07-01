---
description: Generate a local Claude Code cost report from the ECC cost-tracker metrics log.
argument-hint: [csv]
---

# Cost Report

Summarize local Claude Code spend by day, model, and session from the metrics
log that ECC's `stop:cost-tracker` hook writes.

## Where the data lives

The tracker appends one JSON object per session-stop to
`~/.claude/metrics/costs.jsonl`. Each row is a **cumulative snapshot for that
session**, so the report takes the **latest row per `session_id`** and sums
across sessions (summing every row would multiply-count).

Row schema:
`{ timestamp, session_id, transcript_path, model, input_tokens, output_tokens, cache_write_tokens, cache_read_tokens, estimated_cost_usd }`

## What this command does

1. Check that `~/.claude/metrics/costs.jsonl` exists. If it does not, tell the
   user the tracker is not set up yet (it populates after the first session ends
   with the `stop:cost-tracker` hook enabled).
2. Reduce rows to the latest snapshot per session and aggregate.
3. Present a compact report, or export recent rows as CSV when the argument is `csv`.

`node` is used instead of `sqlite3`/`jq` so this works identically on macOS,
Linux, and Windows.

## Report

```bash
ECC_ROOT="${CLAUDE_PLUGIN_ROOT:-$(node -e "var r=(()=>{var e=process.env.ECC_ROOT;if(e&&e.trim())return e.trim();var p=require('path'),f=require('fs'),h=require('os').homedir(),d=p.join(h,'.claude'),q=p.join('scripts','record_invocation.py');if(f.existsSync(p.join(d,q)))return d;for(var s of [['ecc'],['ecc@ecc'],['marketplaces','ecc'],['everything-claude-code'],['everything-claude-code@everything-claude-code'],['marketplaces','everything-claude-code']]){var l=p.join(d,'plugins',...s);if(f.existsSync(p.join(l,q)))return l}try{for(var g of ['ecc','everything-claude-code']){var b=p.join(d,'plugins','cache',g);for(var o of f.readdirSync(b,{withFileTypes:true})){if(!o.isDirectory())continue;for(var v of f.readdirSync(p.join(b,o.name),{withFileTypes:true})){if(!v.isDirectory())continue;var c=p.join(b,o.name,v.name);if(f.existsSync(p.join(c,q)))return c}}}}catch(x){}return d})();console.log(r)")}"
START_MS=$(node -e "process.stdout.write(String(Date.now()))")
node -e '
const fs=require("fs"),os=require("os"),path=require("path");
const f=path.join(os.homedir(),".claude","metrics","costs.jsonl");
if(!fs.existsSync(f)){console.log("Cost tracker not set up: "+f+" not found. Enable the stop:cost-tracker hook and finish a session first.");process.exit(0);}
const rows=fs.readFileSync(f,"utf8").split(/\r?\n/).filter(Boolean).map(l=>{try{return JSON.parse(l)}catch{return null}}).filter(Boolean);
const bySession=new Map();
for(const r of rows){const k=r.session_id||r.transcript_path||r.timestamp;const p=bySession.get(k);if(!p||String(r.timestamp)>String(p.timestamp))bySession.set(k,r);}
const latest=[...bySession.values()];
const cost=r=>Number(r.estimated_cost_usd)||0;
const day=r=>String(r.timestamp||"").slice(0,10);
const today=new Date().toISOString().slice(0,10);
const d=new Date(Date.now()-864e5).toISOString().slice(0,10);
const sum=a=>a.reduce((s,r)=>s+cost(r),0);
const f4=n=>"$"+n.toFixed(4);
console.log("=== Cost summary ===");
console.log("today:     "+f4(sum(latest.filter(r=>day(r)===today))));
console.log("yesterday: "+f4(sum(latest.filter(r=>day(r)===d))));
console.log("total:     "+f4(sum(latest))+"  ("+latest.length+" sessions)");
const by=(key)=>{const m=new Map();for(const r of latest){const k=key(r)||"(unknown)";m.set(k,(m.get(k)||0)+cost(r));}return [...m.entries()].sort((a,b)=>b[1]-a[1]);};
console.log("\n=== By model ===");for(const [k,v] of by(r=>r.model))console.log(f4(v).padStart(12)+"  "+k);
console.log("\n=== Last 7 days ===");
const days=new Map();for(const r of latest){const k=day(r);days.set(k,(days.get(k)||0)+cost(r));}
[...days.entries()].sort((a,b)=>b[0]<a[0]?-1:1).slice(0,7).forEach(([k,v])=>console.log(k+"  "+f4(v)));
'
REPORT_STATUS=$?
END_MS=$(node -e "process.stdout.write(String(Date.now()))")
DURATION=$((END_MS - START_MS))
SUCCESS=0
if [ "$REPORT_STATUS" -eq 0 ]; then SUCCESS=1; fi
node -e 'const {spawnSync}=require("child_process");const path=require("path");const root=process.argv[1],duration=process.argv[2],success=process.argv[3];const probes=process.platform==="win32"?[{command:"py",prefix:["-3"]},{command:"python",prefix:[]},{command:"python3",prefix:[]}]:[{command:"python3",prefix:[]},{command:"python",prefix:[]},{command:"py",prefix:["-3"]}];for(const probe of probes){const result=spawnSync(probe.command,[...probe.prefix,path.join(root,"scripts","record_invocation.py"),"--name","cost-report","--kind","command","--duration-ms",duration,"--success",success],{stdio:"ignore"});if(result.status===0)process.exit(0);if(result.error&&result.error.code==="ENOENT")continue;process.exit(result.status||1)}' "$ECC_ROOT" "$DURATION" "$SUCCESS" || true
exit "$REPORT_STATUS"
```

## CSV export (`/cost-report csv`)

```bash
ECC_ROOT="${CLAUDE_PLUGIN_ROOT:-$(node -e "var r=(()=>{var e=process.env.ECC_ROOT;if(e&&e.trim())return e.trim();var p=require('path'),f=require('fs'),h=require('os').homedir(),d=p.join(h,'.claude'),q=p.join('scripts','record_invocation.py');if(f.existsSync(p.join(d,q)))return d;for(var s of [['ecc'],['ecc@ecc'],['marketplaces','ecc'],['everything-claude-code'],['everything-claude-code@everything-claude-code'],['marketplaces','everything-claude-code']]){var l=p.join(d,'plugins',...s);if(f.existsSync(p.join(l,q)))return l}try{for(var g of ['ecc','everything-claude-code']){var b=p.join(d,'plugins','cache',g);for(var o of f.readdirSync(b,{withFileTypes:true})){if(!o.isDirectory())continue;for(var v of f.readdirSync(p.join(b,o.name),{withFileTypes:true})){if(!v.isDirectory())continue;var c=p.join(b,o.name,v.name);if(f.existsSync(p.join(c,q)))return c}}}}catch(x){}return d})();console.log(r)")}"
START_MS=$(node -e "process.stdout.write(String(Date.now()))")
node -e '
const fs=require("fs"),os=require("os"),path=require("path");
const f=path.join(os.homedir(),".claude","metrics","costs.jsonl");
if(!fs.existsSync(f)){console.error("no data");process.exit(0);}
const rows=fs.readFileSync(f,"utf8").split(/\r?\n/).filter(Boolean).map(l=>{try{return JSON.parse(l)}catch{return null}}).filter(Boolean).slice(-100);
console.log("timestamp,session_id,model,input_tokens,output_tokens,cache_write_tokens,cache_read_tokens,estimated_cost_usd");
for(const r of rows)console.log([r.timestamp,r.session_id,r.model,r.input_tokens,r.output_tokens,r.cache_write_tokens,r.cache_read_tokens,r.estimated_cost_usd].join(","));
'
REPORT_STATUS=$?
END_MS=$(node -e "process.stdout.write(String(Date.now()))")
DURATION=$((END_MS - START_MS))
SUCCESS=0
if [ "$REPORT_STATUS" -eq 0 ]; then SUCCESS=1; fi
node -e 'const {spawnSync}=require("child_process");const path=require("path");const root=process.argv[1],duration=process.argv[2],success=process.argv[3];const probes=process.platform==="win32"?[{command:"py",prefix:["-3"]},{command:"python",prefix:[]},{command:"python3",prefix:[]}]:[{command:"python3",prefix:[]},{command:"python",prefix:[]},{command:"py",prefix:["-3"]}];for(const probe of probes){const result=spawnSync(probe.command,[...probe.prefix,path.join(root,"scripts","record_invocation.py"),"--name","cost-report","--kind","command","--duration-ms",duration,"--success",success],{stdio:"ignore"});if(result.status===0)process.exit(0);if(result.error&&result.error.code==="ENOENT")continue;process.exit(result.status||1)}' "$ECC_ROOT" "$DURATION" "$SUCCESS" || true
exit "$REPORT_STATUS"
```

## Report format

1. Summary: today, yesterday, total, session count.
2. By model: models ranked by total cost.
3. Last seven days: date and cost.

Rely on the precomputed `estimated_cost_usd` values written by the tracker; do
not re-estimate pricing from raw tokens here.
