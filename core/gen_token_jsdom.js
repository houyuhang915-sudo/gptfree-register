/**
 * 用 jsdom 提供完整 window, eval Sentinel SDK, 生成 turnstile/so proof。
 *
 * 用法: node gen_token_jsdom.js <input.json>
 * input: { chatReq, flow, deviceId, cachedProof }
 * 输出末尾: === JSON_OUTPUT ===\n{ t, so, flow, deviceId }
 */
const { JSDOM } = require("jsdom");
const fs = require("fs");
const os = require("os");
const path = require("path");
const crypto = require("crypto");

const ROOT = __dirname;

function loadSdkCode() {
  const candidates = [
    path.join(ROOT, "sentinel_vm", "sentinel_sdk_full.js"),
    path.join(ROOT, "sentinel_vm", "sdk.js"),
    path.join(ROOT, "sentinel_sdk_full.js"),
    path.join(os.homedir(), ".codeium", "windsurf", "sentinel_sdk_full.js"),
  ];
  let raw = null;
  let used = null;
  for (const p of candidates) {
    if (fs.existsSync(p)) {
      raw = fs.readFileSync(p, "utf-8");
      used = p;
      break;
    }
  }
  if (!raw) {
    console.error("SDK not found. Tried:", candidates.join(" | "));
    process.exit(1);
  }
  console.log("SDK path:", used, "bytes:", raw.length);

  let sdkCode = raw.trim();
  // Legacy windsurf dump: whole file is a JSON string literal
  if (sdkCode.startsWith('"') && sdkCode.includes("var SentinelSDK")) {
    sdkCode = sdkCode
      .replace(/^"var SentinelSDK/, "var SentinelSDK")
      .replace(/\\n"$/, "")
      .replace(/\\"/g, '"')
      .replace(/\\n/g, "\n")
      .replace(/\\\\/g, "\\");
  }
  // Strip wrapping quotes if still present
  if (sdkCode.startsWith('"') && sdkCode.endsWith('"')) {
    try {
      sdkCode = JSON.parse(sdkCode);
    } catch (_) {
      /* keep as-is */
    }
  }
  return sdkCode;
}

let sdkCode = loadSdkCode();

// Hook: 在 SDK IIFE 末尾 (t.token=ye 后) 暴露内部函数
// SDK 结尾: t.sessionObserverToken=async function...; t.token=ye; t}({});
let hookedCode = sdkCode.replace(
  "t.token=ye,t}({});",
  "t.___n=_n,t.__Nt=Nt,t.__D=D,t.__$=$,t.token=ye,t}({});"
);

if (hookedCode === sdkCode) {
  console.error("WARNING: Hook replacement did not match!");
  const idx = sdkCode.indexOf("t.token=ye");
  console.error("t.token=ye at pos:", idx);
  if (idx >= 0) console.error("Context:", sdkCode.substring(idx, idx + 40));
  // last-ditch: expose via alternate minified endings
  const alt = sdkCode.replace(
    /t\.token=ye,t\}\(\{\}\);/,
    "t.___n=_n,t.__Nt=Nt,t.__D=D,t.__$=$,t.token=ye,t}({});"
  );
  if (alt !== sdkCode) {
    hookedCode = alt;
    console.log("Hook replacement OK (regex)");
  }
} else {
  console.log("Hook replacement OK");
}

const dom = new JSDOM(`<!DOCTYPE html><html><body></body></html>`, {
  url: "https://auth.openai.com/about-you",
  referrer: "https://auth.openai.com/about-you",
  contentType: "text/html",
  runScripts: "outside-only",
  pretendToBeVisual: true,
});

const { window } = dom;

if (!window.crypto) window.crypto = {};
window.crypto.getRandomValues = (arr) => {
  const buf = crypto.randomBytes(arr.length);
  for (let i = 0; i < arr.length; i++) arr[i] = buf[i];
  return arr;
};
if (!window.crypto.randomUUID) {
  window.crypto.randomUUID = () => crypto.randomUUID();
}

if (!window.performance.memory) {
  window.performance.memory = {
    jsHeapSizeLimit: 4294705152,
    totalJSHeapSize: 35000000,
    usedJSHeapSize: 25000000,
  };
}

const vm = require("vm");
const context = dom.getInternalVMContext();

try {
  vm.runInContext(hookedCode, context, { filename: "sentinel_sdk.js" });
} catch (e) {
  console.error("SDK run error:", e.message);
  console.error(e.stack?.substring(0, 500));
}

console.log("SentinelSDK:", typeof window.SentinelSDK);
console.log("___n:", typeof window.SentinelSDK?.___n);
console.log("__Nt:", typeof window.SentinelSDK?.__Nt);
console.log("__D:", typeof window.SentinelSDK?.__D);
console.log("__$:", typeof window.SentinelSDK?.__$);

if (typeof window.SentinelSDK?.___n !== "function") {
  console.error("Failed to extract _n");
  process.exit(1);
}

const _n = window.SentinelSDK.___n;
const Nt = window.SentinelSDK.__Nt;
const D = window.SentinelSDK.__D;
const inputPath = process.argv[2];
if (!inputPath) {
  console.error("Usage: node gen_token_jsdom.js <input.json>");
  process.exit(1);
}
const input = JSON.parse(fs.readFileSync(inputPath, "utf-8"));
const { chatReq, flow, deviceId, cachedProof } = input;

console.log("\n--- Testing turnstile VM ---");
console.log("dx length:", chatReq?.turnstile?.dx?.length || 0);
console.log("proof:", String(cachedProof || "").substring(0, 50) + "...");

if (typeof D === "function" && chatReq && cachedProof) {
  try {
    D(chatReq, cachedProof);
    console.log("WeakMap set OK");
  } catch (e) {
    console.error("WeakMap set error:", e.message);
  }
}

const dx = chatReq?.turnstile?.dx;
if (!dx) {
  console.error("No turnstile.dx in chatReq");
  const output = { t: null, so: null, flow, deviceId };
  console.log("\n=== JSON_OUTPUT ===");
  console.log(JSON.stringify(output));
  process.exit(0);
}

_n(chatReq, dx)
  .then((result) => {
    console.log("\nTurnstile result:");
    console.log("  type:", typeof result);
    console.log("  length:", String(result).length);
    console.log("  preview:", String(result).substring(0, 100));

    const finish = (soResult) => {
      const output = { t: result, so: soResult || null, flow, deviceId };
      console.log("\n=== JSON_OUTPUT ===");
      console.log(JSON.stringify(output));
      process.exit(0);
    };

    if (typeof Nt === "function" && chatReq.so?.collector_dx) {
      console.log("\n--- Testing SO VM ---");
      Nt(chatReq.so.collector_dx)
        .then((soResult) => {
          console.log("SO result length:", String(soResult).length);
          finish(soResult);
        })
        .catch((e) => {
          console.error("SO VM error:", e.message);
          finish(null);
        });
    } else {
      finish(null);
    }
  })
  .catch((e) => {
    console.error("Turnstile VM error:", e.message);
    process.exit(1);
  });

setTimeout(() => {
  console.error("Timeout: 30s");
  process.exit(1);
}, 30000);
