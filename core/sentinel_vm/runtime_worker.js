'use strict';

const cryptoMod = require('node:crypto');
const fs = require('node:fs');
const vm = require('node:vm');
const { TextEncoder } = require('node:util');

const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const listeners = new Map();
let iframeObject = null;
let capturedProof = null;

function addListener(type, callback) {
  if (typeof callback !== 'function') return;
  const bucket = listeners.get(type) || [];
  bucket.push(callback);
  listeners.set(type, bucket);
}

function removeListener(type, callback) {
  const bucket = listeners.get(type) || [];
  listeners.set(type, bucket.filter((item) => item !== callback));
}

async function dispatch(type, init = {}) {
  const event = {
    type,
    bubbles: true,
    cancelable: true,
    defaultPrevented: false,
    timeStamp: performance.now(),
    preventDefault() { this.defaultPrevented = true; },
    stopPropagation() {},
    ...init,
  };
  for (const callback of [...(listeners.get(type) || [])]) {
    await callback.call(context.window, event);
  }
}

function storageMock(initial = {}) {
  const storage = {};
  const values = new Map();
  const syncKey = (key) => {
    Object.defineProperty(storage, key, {
      configurable: true,
      enumerable: true,
      get: () => values.get(key),
      set: (value) => values.set(key, String(value)),
    });
  };
  for (const [key, value] of Object.entries(initial)) {
    values.set(key, String(value));
    syncKey(key);
  }
  Object.defineProperties(storage, {
    length: { enumerable: false, get: () => values.size },
    key: { enumerable: false, value: (index) => [...values.keys()][index] ?? null },
    getItem: { enumerable: false, value: (key) => values.has(String(key)) ? values.get(String(key)) : null },
    setItem: { enumerable: false, value: (key, value) => {
      key = String(key);
      values.set(key, String(value));
      syncKey(key);
    } },
    removeItem: { enumerable: false, value: (key) => {
      key = String(key);
      values.delete(key);
      delete storage[key];
    } },
    clear: { enumerable: false, value: () => {
      for (const key of values.keys()) delete storage[key];
      values.clear();
    } },
  });
  return storage;
}

const profile = input.profile || {};
const navigatorProfile = profile.navigator || {};
const screenProfile = profile.screen || {};
const webglProfile = profile.webgl || {};
const rectProfile = (profile.dom || {}).rect || {};
const pageUrl = profile.url || 'https://auth.openai.com/about-you';
const language = navigatorProfile.language || input.language || 'en-US';
const languages = navigatorProfile.languages || [language, 'en'];

function genericElement(tagName) {
  const elementListeners = new Map();
  return {
    tagName: String(tagName || '').toUpperCase(),
    style: {},
    children: [],
    innerText: '',
    textContent: '',
    appendChild(child) { this.children.push(child); return child; },
    removeChild(child) {
      const index = this.children.indexOf(child);
      if (index >= 0) this.children.splice(index, 1);
      return child;
    },
    remove() {},
    setAttribute() {},
    getAttribute() { return null; },
    addEventListener(type, callback) {
      const bucket = elementListeners.get(type) || [];
      bucket.push(callback);
      elementListeners.set(type, bucket);
    },
    removeEventListener(type, callback) {
      const bucket = elementListeners.get(type) || [];
      elementListeners.set(type, bucket.filter((item) => item !== callback));
    },
    getBoundingClientRect() {
      return {
        x: rectProfile.x ?? 8,
        y: rectProfile.y ?? 8,
        width: rectProfile.width ?? 1350,
        height: rectProfile.height ?? 24,
        top: rectProfile.top ?? 8,
        right: rectProfile.right ?? 1358,
        bottom: rectProfile.bottom ?? 32,
        left: rectProfile.left ?? 8,
        toJSON() { return this; },
      };
    },
  };
}

function canvasElement() {
  const element = genericElement('canvas');
  element.width = 300;
  element.height = 150;
  element.getContext = (kind) => {
    if (!['webgl', 'experimental-webgl', 'webgl2'].includes(kind)) return null;
    const debugInfo = {
      UNMASKED_VENDOR_WEBGL: 0x9245,
      UNMASKED_RENDERER_WEBGL: 0x9246,
    };
    return {
      VENDOR: 0x1F00,
      RENDERER: 0x1F01,
      getExtension(name) { return name === 'WEBGL_debug_renderer_info' ? debugInfo : null; },
      getParameter(parameter) {
        if (parameter === debugInfo.UNMASKED_VENDOR_WEBGL || parameter === 0x1F00) {
          return webglProfile.vendor || 'Google Inc. (Intel)';
        }
        if (parameter === debugInfo.UNMASKED_RENDERER_WEBGL || parameter === 0x1F01) {
          return webglProfile.renderer || 'ANGLE (Intel, Intel(R) UHD Graphics Direct3D11 vs_5_0 ps_5_0, D3D11)';
        }
        return 0;
      },
    };
  };
  return element;
}

const context = {
  console: { log() {}, error() {}, warn() {} },
  setTimeout,
  clearTimeout,
  setInterval,
  clearInterval,
  queueMicrotask,
  Promise,
  URL,
  URLSearchParams,
  Math,
  Date,
  JSON,
  Array,
  Object,
  String,
  Number,
  Boolean,
  RegExp,
  Function,
  Symbol,
  Reflect,
  Proxy,
  Error,
  TypeError,
  Map,
  Set,
  WeakMap,
  WeakSet,
  Uint8Array,
  TextEncoder,
  btoa: (value) => Buffer.from(String(value), 'binary').toString('base64'),
  atob: (value) => Buffer.from(String(value), 'base64').toString('binary'),
  unescape,
  encodeURIComponent,
  decodeURIComponent,
  parseInt,
  parseFloat,
  isFinite,
  isNaN,
  Intl,
  crypto: cryptoMod.webcrypto,
  performance: {
    now: () => performance.now(),
    timeOrigin: performance.timeOrigin,
    memory: { jsHeapSizeLimit: 4294967296 },
  },
  screen: {
    width: screenProfile.width || 1920,
    height: screenProfile.height || 1080,
    availWidth: screenProfile.availWidth || 1920,
    availHeight: screenProfile.availHeight || 1040,
    availLeft: screenProfile.availLeft || 0,
    availTop: screenProfile.availTop || 0,
    colorDepth: screenProfile.colorDepth || 24,
    pixelDepth: screenProfile.pixelDepth || 24,
  },
  navigator: {
    userAgent: navigatorProfile.userAgent || input.userAgent,
    language,
    languages,
    platform: navigatorProfile.platform || 'Win32',
    vendor: navigatorProfile.vendor || 'Google Inc.',
    hardwareConcurrency: navigatorProfile.hardwareConcurrency || 8,
    deviceMemory: navigatorProfile.deviceMemory || 8,
    maxTouchPoints: navigatorProfile.maxTouchPoints || 0,
    cookieEnabled: true,
    onLine: true,
    storage: {
      estimate: async () => ({
        quota: navigatorProfile.storageQuota || 274877906944,
        usage: navigatorProfile.storageUsage || 0,
      }),
    },
  },
  history: { length: profile.historyLength || 3 },
  localStorage: storageMock(profile.localStorage || {
    'oai/apps/capabilities': '{}',
    'oai/language': language,
    'oai/sidebar-state': 'true',
  }),
  sessionStorage: storageMock(profile.sessionStorage || {}),
  innerWidth: screenProfile.innerWidth || 1903,
  innerHeight: screenProfile.innerHeight || 969,
  outerWidth: screenProfile.outerWidth || 1920,
  outerHeight: screenProfile.outerHeight || 1080,
  devicePixelRatio: screenProfile.devicePixelRatio || 1,
  scrollX: 0,
  scrollY: 0,
  requestAnimationFrame: (callback) => setTimeout(() => callback(performance.now()), 16),
  cancelAnimationFrame: clearTimeout,
  getComputedStyle: () => ({ fontFamily: 'Arial', fontSize: '16px', display: 'block' }),
  matchMedia: () => ({ matches: false, addEventListener() {}, removeEventListener() {} }),
};

context.window = context;
context.globalThis = context;
context.self = context;
context.top = context;
context.parent = context;
context.location = {
  href: pageUrl,
  origin: new URL(pageUrl).origin,
  pathname: new URL(pageUrl).pathname,
  search: new URL(pageUrl).search,
  hash: new URL(pageUrl).hash,
  toString() { return this.href; },
};
context.addEventListener = addListener;
context.removeEventListener = removeListener;
context.dispatchEvent = (event) => dispatch(event.type, event);
context.__dispatch = dispatch;
context.postMessage = () => {};
context.chrome = { runtime: {}, app: {} };
context.Reflect = Reflect;

const documentElement = genericElement('html');
documentElement.getAttribute = (name) => name === 'data-build' ? (profile.build || null) : null;
const body = genericElement('body');
body.appendChild = (element) => {
  body.children.push(element);
  if (element === iframeObject) {
    setTimeout(() => { for (const callback of iframeObject._load || []) callback(); }, 0);
  }
  return element;
};
context.document = {
  cookie: input.documentCookie || profile.cookie || '',
  referrer: profile.referer || 'https://auth.openai.com/',
  URL: pageUrl,
  location: context.location,
  visibilityState: 'visible',
  hidden: false,
  readyState: 'complete',
  currentScript: { src: input.sdkUrl },
  scripts: [{ src: input.sdkUrl }],
  documentElement,
  body,
  head: genericElement('head'),
  addEventListener: addListener,
  removeEventListener: removeListener,
  dispatchEvent: (event) => dispatch(event.type, event),
  querySelector() { return null; },
  querySelectorAll() { return []; },
  createElement(tagName) {
    if (tagName === 'canvas') return canvasElement();
    if (tagName !== 'iframe') return genericElement(tagName);
    iframeObject = genericElement('iframe');
    iframeObject._load = [];
    iframeObject.addEventListener = (type, callback) => {
      if (type === 'load') iframeObject._load.push(callback);
    };
    iframeObject.contentWindow = {
      postMessage: async (message, origin) => {
        capturedProof = message.p;
        const result = input.mode === 'token'
          ? { cachedChatReq: input.cachedChatReq, cachedProof: input.cachedProof || message.p }
          : null;
        const event = {
          source: iframeObject.contentWindow,
          data: { type: 'response', requestId: message.requestId, result },
          origin,
        };
        for (const callback of listeners.get('message') || []) callback(event);
      },
    };
    return iframeObject;
  },
};

function randomInt(min, max) {
  return min + cryptoMod.randomInt(Math.max(1, max - min + 1));
}

async function dispatchBehavior(durationMs) {
  const started = Date.now();
  const moves = randomInt(12, 16);
  let x = randomInt(260, 420);
  let y = randomInt(180, 300);
  for (let index = 0; index < moves; index += 1) {
    const dx = randomInt(5, 18);
    const dy = randomInt(-4, 12);
    x += dx;
    y += dy;
    await new Promise((resolve) => setTimeout(resolve, randomInt(70, 145)));
    await dispatch('pointermove', {
      clientX: x, clientY: y, screenX: x, screenY: y,
      movementX: dx, movementY: dy, buttons: 0,
    });
  }
  await new Promise((resolve) => setTimeout(resolve, randomInt(90, 220)));
  await dispatch('click', { clientX: x, clientY: y, screenX: x, screenY: y, button: 0, buttons: 0 });
  for (let index = 0; index < randomInt(3, 4); index += 1) {
    await new Promise((resolve) => setTimeout(resolve, randomInt(80, 180)));
    context.scrollY += randomInt(35, 120);
    await dispatch('scroll', { scrollX: 0, scrollY: context.scrollY });
  }
  await new Promise((resolve) => setTimeout(resolve, randomInt(80, 160)));
  await dispatch('wheel', { deltaX: 0, deltaY: randomInt(70, 140), clientX: x, clientY: y });
  const keys = ['L', 'u', 'Tab'];
  for (const key of keys) {
    await new Promise((resolve) => setTimeout(resolve, randomInt(90, 210)));
    await dispatch('keydown', {
      key,
      code: key === 'Tab' ? 'Tab' : `Key${key.toUpperCase()}`,
      repeat: false,
      altKey: false,
      ctrlKey: false,
      metaKey: false,
    });
  }
  const remaining = Math.max(0, Number(durationMs || 0) - (Date.now() - started));
  if (remaining) await new Promise((resolve) => setTimeout(resolve, remaining));
}

vm.createContext(context);
vm.runInContext(input.sdk, context, { timeout: 5000 });

(async () => {
  if (input.mode === 'proof') {
    await context.SentinelSDK.init(input.flow);
    process.stdout.write(JSON.stringify({ proof: capturedProof }));
    return;
  }
  const token = await context.SentinelSDK.token(input.flow);
  await dispatchBehavior(input.behaviorDurationMs ?? 4200);
  const soToken = await context.SentinelSDK.sessionObserverToken(input.flow);
  process.stdout.write(JSON.stringify({ token, soToken }));
})().catch((error) => {
  process.stderr.write(String(error && error.stack || error));
  process.exit(2);
});
