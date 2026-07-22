/**
 * ChatGPT 注册页面自动化 JS（参照 FlowPilot/content/signup-page.js 实现）
 * 设计:
 *   - JS 端只暴露同步/异步动作，不做循环或长等待
 *   - Python 端通过 __gpt_getPageState() 拿到当前状态，按状态派发动作
 */
(function () {
    'use strict';

    var SIGNUP_TAG = '__gpt_signup_v2_loaded__';
    if (window[SIGNUP_TAG]) return;
    window[SIGNUP_TAG] = true;

    var log = function (s) { try { console.log('[GPT-SIGNUP] ' + s); } catch (_) { } };

    // ====== FlowPilot 一致的正则 ======
    var SIGNUP_ENTRY_TRIGGER_PATTERN = /免费注册|立即注册|注册|sign\s*up|register|create\s*account|create\s+account|無料(?:で)?サインアップ|サインアップ|新規登録|登録/i;
    var SIGNUP_SWITCH_TO_EMAIL_PATTERN = /继续使用(?:电子邮件地址|电子邮箱|邮箱)(?:登录|注册)?|改用(?:电子邮件地址|电子邮箱|邮箱)(?:登录|注册)?|continue\s+using\s+(?:an?\s+)?email(?:\s+address)?(?:\s+(?:to\s+)?(?:log\s*in|sign\s*in|sign\s*up))?|continue\s+with\s+email(?:\s+address)?|use\s+(?:an?\s+)?email(?:\s+address)?(?:\s+instead)?|sign\s*(?:in|up)\s+with\s+email|メール(?:アドレス)?(?:で|を使って)(?:続ける|登録|サインアップ)|メールアドレスで続ける/i;
    var SIGNUP_SWITCH_ACTION_PATTERN = /继续使用|改用|continue|use|sign\s*(?:in|up)|続ける|使用する/i;
    var SIGNUP_EMAIL_ACTION_PATTERN = /电子邮件|邮箱|email/i;
    var SIGNUP_PHONE_ACTION_PATTERN = /手机|手机号|电话号码|phone|telephone|mobile/i;
    var SIGNUP_MORE_OPTIONS_PATTERN = /更多选项|其它方式|其他方式|more\s+options|show\s+more|other\s+(?:options|ways)/i;
    var SIGNUP_WORK_EMAIL_PATTERN = /工作|business|work\s+email/i;
    var CONTINUE_ACTION_PATTERN = /^\s*(?:continue|next|submit|继续|下一步|创建账户|create\s+account|sign\s*up|完成(?:帐户)?(?:创建)?|finish|done)\s*$/i;
    var OAUTH_CONSENT_PAGE_PATTERN = /codex|chatgpt\s+(?:plus|app|will|要求|wants?|requests?)|授权|authorize/i;

    var SIGNUP_EMAIL_SELECTOR = [
        'input[type="email"]',
        'input[autocomplete="email"]',
        'input[autocomplete="username"]',
        'input[name="email"]',
        'input[name="username"]',
        'input[id*="email"]',
        'input[placeholder*="email" i]',
        'input[placeholder*="电子邮件"]',
        'input[placeholder*="邮箱"]',
        'input[aria-label*="email" i]',
        'input[aria-label*="电子邮件"]',
        'input[aria-label*="邮箱"]'
    ].join(', ');

    var SIGNUP_PHONE_SELECTOR = [
        'input[type="tel"]:not([maxlength="6"]):not([maxlength="1"])',
        'input[name*="phone" i]',
        'input[id*="phone" i]',
        'input[autocomplete="tel"]',
        'input[placeholder*="手机"]',
        'input[aria-label*="手机"]'
    ].join(', ');

    // ====== DOM 工具 ======
    function isVisible(el) {
        if (!el) return false;
        var style = window.getComputedStyle(el);
        if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
        var rect = el.getBoundingClientRect();
        return rect.width > 0 && rect.height > 0;
    }

    function isEnabled(el) {
        return Boolean(el) && !el.disabled && el.getAttribute('aria-disabled') !== 'true';
    }

    function getText(el) {
        if (!el) return '';
        return [el.textContent, el.value, el.getAttribute && el.getAttribute('aria-label'), el.getAttribute && el.getAttribute('title')]
            .filter(Boolean).join(' ').replace(/\s+/g, ' ').trim();
    }

    function getPageText() {
        return (document.body && (document.body.innerText || document.body.textContent) || '')
            .replace(/\s+/g, ' ').trim();
    }

    function dispatchPointer(el) {
        var rect = el.getBoundingClientRect();
        var x = rect.left + rect.width / 2;
        var y = rect.top + rect.height / 2;
        ['pointerover', 'pointerenter', 'mouseover', 'mouseenter',
            'pointermove', 'mousemove', 'pointerdown', 'mousedown',
            'pointerup', 'mouseup', 'click'].forEach(function (type) {
                try {
                    var EventCtor = type.indexOf('pointer') === 0 ? PointerEvent : MouseEvent;
                    el.dispatchEvent(new EventCtor(type, {
                        bubbles: true, cancelable: true, view: window,
                        clientX: x, clientY: y, button: 0,
                        pointerId: 1, pointerType: 'mouse'
                    }));
                } catch (_) { }
            });
    }

    function clickEl(el) {
        if (!el) return false;
        try { el.scrollIntoView({ block: 'center', behavior: 'auto' }); } catch (_) { }
        try { el.focus && el.focus(); } catch (_) { }
        dispatchPointer(el);
        try { el.click(); } catch (_) { }
        log('clicked: ' + getText(el).slice(0, 60));
        return true;
    }

    function fillInput(el, value) {
        if (!el) return false;
        try { el.focus(); } catch (_) { }
        try {
            var setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
            setter.call(el, value);
        } catch (_) {
            el.value = value;
        }
        el.dispatchEvent(new Event('input', { bubbles: true }));
        el.dispatchEvent(new Event('change', { bubbles: true }));
        log('filled [' + (el.name || el.id || el.type) + ']');
        return true;
    }

    function findFirst(selectors, filter) {
        var nodes = document.querySelectorAll(selectors);
        for (var i = 0; i < nodes.length; i++) {
            if (!isVisible(nodes[i])) continue;
            if (filter && !filter(nodes[i])) continue;
            return nodes[i];
        }
        return null;
    }

    function findClickable(patterns, opts) {
        opts = opts || {};
        var nodes = document.querySelectorAll('button, a, [role="button"], [role="link"], input[type="button"], input[type="submit"]');
        for (var i = 0; i < nodes.length; i++) {
            var el = nodes[i];
            if (!isVisible(el)) continue;
            if (!opts.allowDisabled && !isEnabled(el)) continue;
            var text = getText(el);
            if (!text) continue;
            for (var j = 0; j < patterns.length; j++) {
                if (patterns[j].test(text)) return el;
            }
        }
        return null;
    }

    // ====== 关键元素查找 ======
    function getEmailInput() {
        var input = document.querySelector(SIGNUP_EMAIL_SELECTOR);
        if (input && isVisible(input)) return input;
        var inputs = document.querySelectorAll('input');
        for (var i = 0; i < inputs.length; i++) {
            var el = inputs[i];
            if (!isVisible(el)) continue;
            var type = (el.getAttribute('type') || '').toLowerCase();
            var ac = (el.getAttribute('autocomplete') || '').toLowerCase();
            var name = (el.getAttribute('name') || '').toLowerCase();
            var id = (el.getAttribute('id') || '').toLowerCase();
            var ph = el.getAttribute('placeholder') || '';
            var aria = el.getAttribute('aria-label') || '';
            if (type === 'email' || ac === 'email' || ac === 'username'
                || /email|username/i.test(name + ' ' + id)
                || /email|电子邮件|邮箱/i.test(ph + ' ' + aria)) {
                return el;
            }
        }
        return null;
    }

    function getPhoneInput() {
        var input = document.querySelector(SIGNUP_PHONE_SELECTOR);
        if (input && isVisible(input)) return input;
        var inputs = document.querySelectorAll('input');
        for (var i = 0; i < inputs.length; i++) {
            var el = inputs[i];
            if (!isVisible(el)) continue;
            var type = (el.getAttribute('type') || '').toLowerCase();
            var maxLen = el.getAttribute('maxlength') || '';
            if (maxLen === '1' || maxLen === '6') continue;
            var ac = (el.getAttribute('autocomplete') || '').toLowerCase();
            var name = (el.getAttribute('name') || '').toLowerCase();
            var id = (el.getAttribute('id') || '').toLowerCase();
            var ph = el.getAttribute('placeholder') || '';
            var aria = el.getAttribute('aria-label') || '';
            if (type === 'tel' || ac === 'tel'
                || /phone|tel/i.test(name + ' ' + id)
                || /手机|电话|手机号/.test(ph + ' ' + aria)) {
                return el;
            }
        }
        return null;
    }

    function getPasswordInput() {
        var inputs = document.querySelectorAll('input[type="password"]');
        for (var i = 0; i < inputs.length; i++) {
            if (isVisible(inputs[i])) return inputs[i];
        }
        return null;
    }

    function getVerificationTarget() {
        // 1) 单输入框（autocomplete=one-time-code 等）
        var single = document.querySelector(
            'input[autocomplete="one-time-code"], input[name="code"], input[name="otp"], input[type="tel"][maxlength="6"], input[type="text"][maxlength="6"]'
        );
        if (single && isVisible(single)) {
            return { type: 'single', element: single };
        }
        // 2) 6 个 maxlength=1 的格子
        var grid = [];
        document.querySelectorAll('input[maxlength="1"]').forEach(function (el) {
            if (isVisible(el)) grid.push(el);
        });
        if (grid.length >= 6) return { type: 'split', elements: grid.slice(0, 6) };
        return null;
    }

    function findSignupEntryTrigger() {
        var nodes = document.querySelectorAll('a, button, [role="button"], [role="link"]');
        var hidden = null;
        for (var i = 0; i < nodes.length; i++) {
            var el = nodes[i];
            if (!isEnabled(el)) continue;
            if (!SIGNUP_ENTRY_TRIGGER_PATTERN.test(getText(el))) continue;
            if (isVisible(el)) return el;
            if (!hidden) hidden = el;
        }
        return hidden;
    }

    function findUseEmailTrigger() {
        var nodes = document.querySelectorAll('button, a, [role="button"], [role="link"]');
        for (var i = 0; i < nodes.length; i++) {
            var el = nodes[i];
            if (!isVisible(el) || !isEnabled(el)) continue;
            var t = getText(el);
            if (!t || SIGNUP_WORK_EMAIL_PATTERN.test(t)) continue;
            if (SIGNUP_SWITCH_TO_EMAIL_PATTERN.test(t)) return el;
            if (SIGNUP_SWITCH_ACTION_PATTERN.test(t) && SIGNUP_EMAIL_ACTION_PATTERN.test(t)) return el;
        }
        return null;
    }

    function findMoreOptionsTrigger() {
        var nodes = document.querySelectorAll('button, a, [role="button"], [role="link"]');
        for (var i = 0; i < nodes.length; i++) {
            var el = nodes[i];
            if (!isVisible(el) || !isEnabled(el)) continue;
            if (!SIGNUP_MORE_OPTIONS_PATTERN.test(getText(el))) continue;
            var expanded = (el.getAttribute('aria-expanded') || '').toLowerCase();
            if (expanded === 'true') continue;
            return el;
        }
        return null;
    }

    function getContinueButton(opts) {
        opts = opts || {};
        var direct = document.querySelector('button[type="submit"], input[type="submit"]');
        if (direct && isVisible(direct) && (opts.allowDisabled || isEnabled(direct))) return direct;
        var btn = findClickable([CONTINUE_ACTION_PATTERN], { allowDisabled: opts.allowDisabled });
        if (btn) return btn;
        return findClickable([/continue|next|submit|继续|下一步|完成|finish|done/i], { allowDisabled: opts.allowDisabled });
    }

    function getOAuthConsentForm() {
        // 真正的同意页表单（仅 auth.openai.com 上）
        var form = document.querySelector('form[action*="authorize" i], form[action*="consent" i]');
        if (form && isVisible(form)) return form;
        return null;
    }

    function isOAuthConsentPage() {
        // 只在 auth.openai.com 系列子域上才认为是 OAuth 同意页
        var host = location.hostname || '';
        if (!/auth\.openai\.com|auth0\.openai\.com|accounts\.openai\.com/.test(host)) return false;
        if (getOAuthConsentForm()) return true;
        var pageText = getPageText();
        // 同时含 "授权/authorize" + "继续/Continue" + "Codex 或 ChatGPT" 才算
        if (/(?:授权|authorize|allow|grant\s+access)/i.test(pageText)
            && /(?:codex|chatgpt|openai)/i.test(pageText)
            && getContinueButton()) {
            return true;
        }
        return false;
    }

    function isAddPhonePage() {
        if (/\/add-phone(?:[/?#]|$)/i.test(location.pathname || '')) return true;
        var form = document.querySelector('form[action*="/add-phone" i]');
        return Boolean(form && isVisible(form));
    }

    function isAddEmailPage() {
        if (/\/add-email(?:[/?#]|$)/i.test(location.pathname || '')) return true;
        var form = document.querySelector('form[action*="/add-email" i]');
        return Boolean(form && isVisible(form));
    }

    function isProfilePage() {
        if (/\/(?:create-account\/profile|u\/signup\/profile|signup\/profile|about-you)(?:[/?#]|$)/i.test(location.pathname || '')) return true;
        var nameInput = document.querySelector('input[name="name"], input[autocomplete="name"], input[placeholder*="全名"]');
        return Boolean(nameInput && isVisible(nameInput));
    }

    function isVerificationErrorPage() {
        var title = (document.title || '');
        if (/身份验证错误|verification\s*error|验证失败/i.test(title)) return true;
        var pageText = (document.body && document.body.innerText || '').slice(0, 2000);
        if (/身份验证错误|verification\s*error|验证码(?:错误|无效|过期|invalid|expired)/i.test(pageText)) return true;
        return false;
    }

    function isAuthErrorPage() {
        // chatgpt.com/api/auth/error 或 chatgpt.com/auth/error
        // OpenAI 注册被后端拒签时跳这里（邮箱已存在 / IP 风控 / iCloud relay 被禁等）
        var host = (location.hostname || '').toLowerCase();
        var path = (location.pathname || '').toLowerCase();
        if (!/chatgpt\.com|chat\.openai\.com|auth\.openai\.com/.test(host)) return false;
        return /\/api\/auth\/error|\/auth\/error(?:[/?#]|$)/i.test(path);
    }

    function getAuthErrorDetail() {
        try {
            var u = new URL(location.href);
            return {
                error: u.searchParams.get('error') || '',
                code: u.searchParams.get('code') || '',
                message: u.searchParams.get('message') || '',
                description: u.searchParams.get('error_description') || '',
            };
        } catch (_) {
            return { error: '', code: '', message: '', description: '' };
        }
    }
    window.__gpt_getAuthErrorDetail = getAuthErrorDetail;

    function isVerificationPage() {
        if (isVerificationErrorPage()) return false;
        if (/\/email-verification(?:[/?#]|$)/i.test(location.pathname || '')) return true;
        if (/\/phone-verification(?:[/?#]|$)/i.test(location.pathname || '')) return true;
        return Boolean(getVerificationTarget());
    }

    function isPasswordPage() {
        if (/\/(?:create-account\/password|u\/signup\/password|signup\/password|log-in\/password)(?:[/?#]|$)/i.test(location.pathname || '')) return true;
        return Boolean(getPasswordInput());
    }

    function isLoggedInChatGPT() {
        var host = location.hostname || '';
        if (!/chatgpt\.com|chat\.openai\.com/.test(host)) return false;
        var path = location.pathname || '';
        if (/^\/(?:auth\/|create-account\/|email-verification|log-in|add-phone|add-email|checkout)(?:[/?#]|$)/i.test(path)) return false;

        // 强信号 1：用户级 DOM
        if (document.querySelector('[data-testid="profile-button"]')) return true;
        if (document.querySelector('[data-testid="user-menu-button"]')) return true;
        if (document.querySelector('button[aria-label*="account" i]')) return true;
        if (document.querySelector('nav[aria-label*="对话" i] a, nav[aria-label*="chat history" i] a, nav[aria-label*="チャット履歴" i] a')) return true;
        if (document.querySelector('ol[aria-label*="history" i] li a, ol[aria-label*="履歴" i] li a')) return true;

        // 反向判定：访客主页一定有 [登录/Log in/ログイン] 或 [注册/Sign up/サインアップ] 按钮，
        // 已登录主页一定没有。
        var nodes = document.querySelectorAll('a, button, [role="button"]');
        var loginEntryRe = /^\s*(?:log\s*in|sign\s*in|登录|登入|ログイン|サインイン)\s*$/i;
        var signupEntryRe = /^\s*(?:sign\s*up|register|注册|免费注册|サインアップ|無料(?:で)?サインアップ|新規登録|登録)\s*$/i;
        var hasLoginEntry = false;
        var hasSignupEntry = false;
        for (var i = 0; i < nodes.length; i++) {
            if (!isVisible(nodes[i])) continue;
            var t = getText(nodes[i]);
            if (loginEntryRe.test(t)) hasLoginEntry = true;
            if (signupEntryRe.test(t)) hasSignupEntry = true;
        }
        if (hasLoginEntry || hasSignupEntry || findSignupEntryTrigger()) return false;

        // 没有登录/注册入口 + 在 chatgpt.com 根路径 + 有聊天输入框 → 已登录
        // （新版 UI: contenteditable="true" 的 ProseMirror 编辑器或 textarea）
        var rootPath = (path === '/' || path === '');
        if (rootPath) {
            var hasChatInput = document.querySelector(
                '#prompt-textarea, '
                + '[contenteditable="true"][role="textbox"], '
                + 'textarea[data-testid="chat-input"], '
                + 'form textarea, '
                + 'textarea[placeholder*="质问" i], '
                + 'textarea[placeholder*="message" i], '
                + 'textarea[placeholder*="質問"], '
                + 'div[contenteditable="true"]'
            );
            if (hasChatInput && isVisible(hasChatInput)) return true;
        }

        // 已登录态特征按钮（多语言关键字，新版 UI 有些会出现）
        var loggedInBtnRe = /^\s*(?:升级|upgrade|アップグレード|无料オファー|無料オファー|無料\s*プラン|free\s*offer|临时聊天|temporary\s*chat|一時的なチャット|画像を作成|画像生成|generate\s*image|生成图片|撰写或编辑|記述または編集|查找资料|何かを調べる|search\s*the\s*web)\s*$/i;
        var loggedInBtnHits = 0;
        for (var k = 0; k < nodes.length; k++) {
            if (!isVisible(nodes[k])) continue;
            var tt = getText(nodes[k]);
            if (loggedInBtnRe.test(tt)) loggedInBtnHits++;
        }
        if (loggedInBtnHits >= 2) return true;

        // logout 链接（强信号，多语言）
        var pageText = (document.body && document.body.innerText || '').slice(0, 4000);
        if (/log\s*out|sign\s*out|退出登录|登出|ログアウト|サインアウト/i.test(pageText)) return true;

        return false;
    }

    // ====== 状态机 ======
    // 优先级：注册流程的页面状态 > 已登录判定（避免主页加载完前误判）
    window.__gpt_getPageState = function () {
        var host = location.hostname || '';

        // 1) 明确的注册流程页面（auth.openai.com 子域 / chatgpt.com 路径）优先识别
        if (isAuthErrorPage()) return 'auth_error';
        if (isVerificationErrorPage()) return 'verification_error';
        if (isAddPhonePage()) return 'add_phone_page';
        if (isAddEmailPage()) return 'add_email_page';
        if (isOAuthConsentPage()) return 'oauth_consent';
        if (isVerificationPage()) return 'verification_page';
        if (isProfilePage()) return 'profile_page';
        if (isPasswordPage()) return 'password_page';
        if (getEmailInput()) return 'email_entry';
        if (getPhoneInput()) return 'phone_entry';
        if (findSignupEntryTrigger()) return 'entry_home';

        // 2) 在 chatgpt.com 主页且没有任何注册入口 → 检查是否已登录（强信号）
        if (/chatgpt\.com|chat\.openai\.com/.test(host) && isLoggedInChatGPT()) {
            return 'logged_in';
        }

        // 3) 加载中
        if (/auth\.openai\.com|auth0\.openai\.com|accounts\.openai\.com/.test(host)) return 'auth_loading';
        if (/chatgpt\.com|chat\.openai\.com/.test(host)) return 'chatgpt_loading';
        return 'unknown';
    };

    // ====== 动作 ======

    // Cookie 弹窗（chatgpt.com 上的）
    window.__gpt_dismissCookie = function () {
        var dialogs = document.querySelectorAll('[role="dialog"], [aria-modal="true"], .cookie-banner, [class*="cookie" i]');
        for (var d = 0; d < dialogs.length; d++) {
            var btns = dialogs[d].querySelectorAll('button, [role="button"]');
            for (var i = 0; i < btns.length; i++) {
                var t = getText(btns[i]).trim();
                if (/^(全部接受|接受全部|接受所有|全部允许|accept\s*all|allow\s*all|reject\s*non[-\s]?essential|拒绝非必需|仅必要|necessary\s*only|i\s*agree)$/i.test(t)) {
                    if (isVisible(btns[i]) && isEnabled(btns[i])) {
                        clickEl(btns[i]);
                        return { clicked: true, text: t };
                    }
                }
            }
        }
        // 顶层按钮
        var top = findClickable([/^(全部接受|接受全部|接受所有|accept\s*all|allow\s*all|reject\s*non[-\s]?essential|拒绝非必需|仅必要)$/i]);
        if (top) { clickEl(top); return { clicked: true, text: getText(top).slice(0, 30) }; }
        return { clicked: false };
    };

    // 点击注册入口（在 chatgpt.com 主页）
    window.__gpt_clickSignupEntry = function () {
        var trigger = findSignupEntryTrigger();
        if (!trigger) return { clicked: false, reason: 'trigger_not_found' };
        clickEl(trigger);
        return { clicked: true, text: getText(trigger).slice(0, 60) };
    };

    // auth 页面里若是手机号模式，切到邮箱
    window.__gpt_switchToEmail = function () {
        var btn = findUseEmailTrigger();
        if (btn) { clickEl(btn); return { clicked: true, text: getText(btn).slice(0, 40) }; }
        var more = findMoreOptionsTrigger();
        if (more) { clickEl(more); return { clicked: true, text: 'more_options' }; }
        return { clicked: false };
    };

    // 填邮箱
    window.__gpt_fillEmail = function (email) {
        var el = getEmailInput();
        if (!el) return { filled: false };
        fillInput(el, email);
        return { filled: true };
    };

    // 填密码
    window.__gpt_fillPassword = function (password) {
        var el = getPasswordInput();
        if (!el) return { filled: false };
        fillInput(el, password);
        return { filled: true };
    };

    // 填验证码
    window.__gpt_fillOTP = function (code) {
        var target = getVerificationTarget();
        if (!target) return { filled: false };
        var setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
        if (target.type === 'single') {
            try { target.element.focus(); } catch (_) { }
            setter.call(target.element, code);
            target.element.dispatchEvent(new Event('input', { bubbles: true }));
            target.element.dispatchEvent(new Event('change', { bubbles: true }));
            try { target.element.dispatchEvent(new KeyboardEvent('keyup', { key: 'Enter', bubbles: true })); } catch (_) { }
            return { filled: true, type: 'single' };
        }
        // split 6 格子
        for (var i = 0; i < 6; i++) {
            var el = target.elements[i];
            try { el.focus(); } catch (_) { }
            try {
                el.dispatchEvent(new KeyboardEvent('keydown', { key: code[i], bubbles: true }));
            } catch (_) { }
            setter.call(el, code[i]);
            el.dispatchEvent(new InputEvent('input', { bubbles: true, data: code[i], inputType: 'insertText' }));
            el.dispatchEvent(new Event('change', { bubbles: true }));
            try { el.dispatchEvent(new KeyboardEvent('keyup', { key: code[i], bubbles: true })); } catch (_) { }
        }
        try { target.elements[5].blur(); } catch (_) { }
        return { filled: true, type: 'split' };
    };

    // 填个人信息（姓名 + 生日 / 年龄）
    window.__gpt_fillProfile = function (fullName, birthday) {
        birthday = birthday || { year: '1995', month: '03', day: '15' };
        var filledAny = false;

        // 1) 姓名（如果有）
        var nameInput = document.querySelector('input[name="name"]')
            || document.querySelector('input[autocomplete="name"]')
            || document.querySelector('input[placeholder*="全名"]')
            || document.querySelector('input[placeholder*="姓名"]')
            || document.querySelector('input[placeholder*="name" i]');
        if (nameInput && isVisible(nameInput)) {
            fillInput(nameInput, fullName || 'James Smith');
            filledAny = true;
        }

        // 2) 年龄（多形态：input[name="age"] / placeholder / aria-label）
        var ageInput = document.querySelector('input[name="age"]')
            || document.querySelector('input[placeholder*="年龄"]')
            || document.querySelector('input[placeholder*="age" i]')
            || document.querySelector('input[aria-label*="年龄"]')
            || document.querySelector('input[aria-label*="age" i]')
            || document.querySelector('input[autocomplete="age"]');
        // 兜底：扫所有 input，找最像"年龄"的
        if (!ageInput) {
            var allInputs = document.querySelectorAll('input[type="text"], input[type="number"], input:not([type])');
            for (var ai = 0; ai < allInputs.length; ai++) {
                var inp = allInputs[ai];
                if (!isVisible(inp)) continue;
                // 跳过姓名/邮箱/密码/OTP
                var nm = (inp.getAttribute('name') || '').toLowerCase();
                var ph = (inp.getAttribute('placeholder') || '').toLowerCase();
                var ar = (inp.getAttribute('aria-label') || '').toLowerCase();
                if (/name|email|password|code|otp|birthday|year|month|day/.test(nm + ph + ar)) continue;
                // 找一个 maxlength 是 2-3 的（年龄字段经常 maxlength=3）
                var ml = inp.getAttribute('maxlength') || '';
                if (ml === '2' || ml === '3') {
                    ageInput = inp;
                    break;
                }
                // 或者 label 文字提示是"年龄"/"age"
                var lbl = '';
                var labelEl = inp.closest('label') || (inp.id && document.querySelector('label[for="' + inp.id + '"]'));
                if (labelEl) lbl = (labelEl.textContent || '').toLowerCase();
                if (/年龄|age/.test(lbl)) {
                    ageInput = inp;
                    break;
                }
            }
        }
        if (ageInput && isVisible(ageInput)) {
            var year = parseInt(birthday.year, 10) || 1995;
            var age = Math.max(18, Math.min(80, new Date().getFullYear() - year));
            fillInput(ageInput, String(age));
            filledAny = true;
        }

        // 3) react-aria spinbutton (生日 month/day/year)
        var yearSpin = document.querySelector('[role="spinbutton"][data-type="year"]');
        var monthSpin = document.querySelector('[role="spinbutton"][data-type="month"]');
        var daySpin = document.querySelector('[role="spinbutton"][data-type="day"]');
        if (yearSpin && monthSpin && daySpin) {
            [[monthSpin, birthday.month], [daySpin, birthday.day], [yearSpin, birthday.year]].forEach(function (pair) {
                var el = pair[0], val = String(pair[1]);
                try { el.focus(); } catch (_) { }
                el.textContent = val;
                el.setAttribute('aria-valuenow', val);
                el.dispatchEvent(new Event('input', { bubbles: true }));
                el.dispatchEvent(new Event('change', { bubbles: true }));
            });
            filledAny = true;
        }

        // 4) 隐藏 input[name="birthday"]（ISO）
        var hidden = document.querySelector('input[name="birthday"]');
        if (hidden) {
            var iso = String(birthday.year) + '-'
                + String(birthday.month).padStart(2, '0') + '-'
                + String(birthday.day).padStart(2, '0');
            try {
                var setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
                setter.call(hidden, iso);
                hidden.dispatchEvent(new Event('input', { bubbles: true }));
                hidden.dispatchEvent(new Event('change', { bubbles: true }));
            } catch (_) { hidden.value = iso; }
            filledAny = true;
        }

        return { filled: filledAny, hadName: !!nameInput, hadAge: !!ageInput, hadBirthday: !!yearSpin };
    };

    // 通用 Continue 按钮
    window.__gpt_clickContinue = function () {
        var btn = getContinueButton();
        if (!btn) {
            var any = findClickable([/continue|next|submit|继续|下一步|创建|create|完成|finish|done|跳过|skip/i]);
            if (any) { clickEl(any); return { clicked: true, fallback: true }; }
            return { clicked: false };
        }
        clickEl(btn);
        return { clicked: true };
    };

    // OAuth 同意页 Continue
    window.__gpt_clickOAuthConfirm = function () {
        var form = getOAuthConsentForm();
        if (form) {
            var submit = form.querySelector('button[type="submit"], button[data-dd-action-name="Continue"]');
            if (submit && isVisible(submit) && isEnabled(submit)) {
                clickEl(submit);
                return { clicked: true, via: 'form_submit' };
            }
        }
        var btn = findClickable([
            /^\s*continue\s*$/i,
            /^\s*allow\s*$/i,
            /^\s*authorize\s*$/i,
            /^\s*agree\s*$/i,
            /^\s*同意\s*$/,
            /^\s*允许\s*$/,
            /^\s*确认\s*$/,
            /^\s*继续\s*$/
        ]);
        if (btn) { clickEl(btn); return { clicked: true }; }
        return { clicked: false };
    };

    // 调试快照
    window.__gpt_debug = function () {
        var btns = document.querySelectorAll('button, a, [role="button"], [role="link"], input[type="submit"]');
        var visibleTexts = [];
        for (var i = 0; i < btns.length && visibleTexts.length < 15; i++) {
            if (isVisible(btns[i])) {
                var t = getText(btns[i]).slice(0, 80);
                if (t) visibleTexts.push(t);
            }
        }
        return {
            ready: true,
            host: location.hostname,
            path: location.pathname,
            url: location.href.slice(0, 200),
            title: document.title,
            buttonCount: btns.length,
            visibleButtons: visibleTexts,
            hasEmailInput: !!getEmailInput(),
            hasPhoneInput: !!getPhoneInput(),
            hasPasswordInput: !!getPasswordInput(),
            hasVerificationInput: !!getVerificationTarget(),
            signupTrigger: findSignupEntryTrigger() ? getText(findSignupEntryTrigger()).slice(0, 60) : null,
            switchToEmail: findUseEmailTrigger() ? getText(findUseEmailTrigger()).slice(0, 60) : null,
            isOAuthConsent: isOAuthConsentPage(),
            isAddPhone: isAddPhonePage(),
            isAddEmail: isAddEmailPage(),
            isProfile: isProfilePage(),
            isVerification: isVerificationPage(),
            isPassword: isPasswordPage(),
            isLoggedIn: isLoggedInChatGPT()
        };
    };

    // 探活
    window.__gpt_ready = function () { return true; };

    log('signup.js v2 注入成功 ' + location.href.slice(0, 80));
})();
