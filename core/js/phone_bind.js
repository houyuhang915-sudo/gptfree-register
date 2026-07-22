/**
 * ChatGPT 绑定手机号自动化 JS（参照 _aBaiAutoplus-ref/platforms/chatgpt/browser_register.py
 * 中的 _submit_add_phone_dom / _submit_phone_otp_dom 同款 DOM 操作，搬到 DrissionPage）。
 *
 * Python 端调用流程：
 *   1. navigate('https://chatgpt.com/auth/login')
 *   2. login（密码或 OTP）→ chatgpt.com 主页
 *   3. navigate('https://auth.openai.com/add-phone')
 *   4. __gpt_pb_submitPhone(phone, dialCode, nationalNumber, countryLabel, isoCode)
 *   5. 拉 OTP → __gpt_pb_submitOtp(code)
 *   6. 等成功跳转
 */
(function () {
    'use strict';

    var TAG = '__gpt_phone_bind_v1_loaded__';
    if (window[TAG]) return;
    window[TAG] = true;

    var log = function (s) { try { console.log('[GPT-PHONE-BIND] ' + s); } catch (_) { } };

    // 已知国码 → ISO（OpenAI add-phone 国家 select 用 ISO 作 value）
    var DIAL_TO_ISO = {
        '1': 'US', '7': 'RU', '20': 'EG', '27': 'ZA',
        '30': 'GR', '31': 'NL', '32': 'BE', '33': 'FR', '34': 'ES',
        '36': 'HU', '39': 'IT', '40': 'RO', '41': 'CH', '43': 'AT',
        '44': 'GB', '45': 'DK', '46': 'SE', '47': 'NO', '48': 'PL',
        '49': 'DE', '51': 'PE', '52': 'MX', '53': 'CU', '54': 'AR',
        '55': 'BR', '56': 'CL', '57': 'CO', '58': 'VE', '60': 'MY',
        '61': 'AU', '62': 'ID', '63': 'PH', '64': 'NZ', '65': 'SG',
        '66': 'TH', '81': 'JP', '82': 'KR', '84': 'VN', '86': 'CN',
        '90': 'TR', '91': 'IN', '92': 'PK', '93': 'AF', '94': 'LK',
        '95': 'MM', '98': 'IR', '212': 'MA', '213': 'DZ', '216': 'TN',
        '218': 'LY', '220': 'GM', '233': 'GH', '234': 'NG', '254': 'KE',
        '256': 'UG', '351': 'PT', '352': 'LU', '353': 'IE', '354': 'IS',
        '358': 'FI', '359': 'BG', '370': 'LT', '371': 'LV', '372': 'EE',
        '380': 'UA', '381': 'RS', '385': 'HR', '386': 'SI', '420': 'CZ',
        '421': 'SK', '852': 'HK', '853': 'MO', '855': 'KH', '856': 'LA',
        '880': 'BD', '886': 'TW', '960': 'MV', '961': 'LB', '962': 'JO',
        '963': 'SY', '964': 'IQ', '965': 'KW', '966': 'SA', '967': 'YE',
        '968': 'OM', '971': 'AE', '972': 'IL', '973': 'BH', '974': 'QA',
        '976': 'MN', '977': 'NP', '992': 'TJ', '993': 'TM', '994': 'AZ',
        '995': 'GE', '998': 'UZ'
    };

    // ====== utils ======
    function visible(el) {
        if (!el) return false;
        var s = window.getComputedStyle(el);
        if (!s) return false;
        if (s.display === 'none' || s.visibility === 'hidden' || s.opacity === '0') return false;
        var r = el.getBoundingClientRect();
        return r.width > 0 && r.height > 0;
    }
    function dispatchIO(el) {
        el.dispatchEvent(new Event('input', { bubbles: true }));
        el.dispatchEvent(new Event('change', { bubbles: true }));
    }
    function setNative(el, value) {
        var proto = el instanceof HTMLTextAreaElement
            ? window.HTMLTextAreaElement.prototype
            : window.HTMLInputElement.prototype;
        var setter = Object.getOwnPropertyDescriptor(proto, 'value');
        if (setter && setter.set) setter.set.call(el, value);
        else el.value = value;
        dispatchIO(el);
    }
    function normalize(s) {
        return String(s || '').replace(/\s+/g, ' ').trim();
    }

    function parseDialAndNational(phone) {
        // phone = '+12025550100' / '+8109078965056' 等
        var s = String(phone || '').trim();
        if (s.charAt(0) !== '+') s = '+' + s;
        var digits = s.replace(/[^\d]/g, '');

        // 优先按 DIAL_TO_ISO 匹配最长前缀
        var keys = Object.keys(DIAL_TO_ISO).sort(function (a, b) { return b.length - a.length; });
        for (var i = 0; i < keys.length; i++) {
            var k = keys[i];
            if (digits.indexOf(k) === 0) {
                var local = digits.slice(k.length);
                // JP 号码：如果 local 以 0 开头是因为输入了 0xxxxxxxxxx 格式（带前导 0），去掉
                if (k === '81' && local.length === 11 && local.charAt(0) === '0') {
                    local = local.slice(1);
                }
                return { dial: k, local: local, iso: DIAL_TO_ISO[k] };
            }
        }
        return { dial: '1', local: digits, iso: 'US' };
    }

    // ====== 状态查询 ======
    window.__gpt_pb_state = function () {
        var path = location.pathname || '';
        var host = location.hostname || '';
        // choose-an-account 页（OAuth 链路里若浏览器记着账号会先弹这个）
        if (/\/choose-an-account|\/choose-account/i.test(path)) return 'choose_account';
        if (/\/add-phone(?:[/?#]|$)/i.test(path)) {
            // 是否已经进入 OTP 阶段
            var otp = document.querySelector('input[autocomplete="one-time-code"], input[name="code"], input[name="otp"], input[type="tel"][maxlength="6"], input[type="text"][maxlength="6"]');
            if (otp && visible(otp)) return 'add_phone_otp';
            var grid = document.querySelectorAll('input[maxlength="1"]');
            var visGrid = 0;
            for (var i = 0; i < grid.length; i++) if (visible(grid[i])) visGrid++;
            if (visGrid >= 6) return 'add_phone_otp';
            return 'add_phone_input';
        }
        if (/\/phone-verification(?:[/?#]|$)/i.test(path)) return 'add_phone_otp';
        // 错误页
        var bodyTxt = (document.body && document.body.innerText || '').slice(0, 4000);
        if (/phone[_\s-]*number[_\s-]*in[_\s-]*use|already\s+in\s+use|phone.*already.*used/i.test(bodyTxt)) {
            return 'phone_in_use';
        }
        if (/(?:invalid|incorrect|expired|wrong).*(?:code|otp|verification)/i.test(bodyTxt)) {
            return 'otp_invalid';
        }
        // 主域已绑成功 → 跳到 chatgpt.com
        if (/chatgpt\.com|chat\.openai\.com/.test(host)) {
            // 不在 add-phone 路径上，认为已离开
            return 'left_add_phone';
        }
        return 'unknown';
    };

    // ====== choose-an-account 页选账户 ======
    // OpenAI 在 OAuth 链路里若浏览器之前登过别的账号，会先要你"选账户"。
    // 优先做法：在 auth_url 上加 login_hint=<email>，这样 OpenAI 直接跳过该页。
    // 这个 helper 是兜底 —— 如果没传 login_hint，或它不生效。
    window.__gpt_pb_pickAccount = function (targetEmail) {
        targetEmail = String(targetEmail || '').trim().toLowerCase();
        // 收集所有可点的、看起来像"账号行"的 button/a/[role=button]
        var clickables = Array.from(document.querySelectorAll(
            'button, a[href], [role="button"]'
        )).filter(visible);

        // 给每个按钮打分，选 email 子串命中、且不是"另一个/创建/取消/关闭"那种
        var EXCLUDE_RE = /(?:another|另一个|create.*account|创建.*帐户|创建.*账户|取消|关闭|cancel|close|×|✕)/i;
        var best = null;
        var bestScore = -1;
        for (var i = 0; i < clickables.length; i++) {
            var el = clickables[i];
            var t = (el.textContent || '').replace(/\s+/g, ' ').trim();
            if (!t) continue;
            if (EXCLUDE_RE.test(t)) continue;
            // 文本太长（命中整个 body）的打负分；纯文本短、含 email 的打高分
            var score = 0;
            if (targetEmail && t.toLowerCase().indexOf(targetEmail) >= 0) {
                score += 100 - Math.min(t.length, 200);   // 越短越好
            }
            // button + 名字看起来像 "Name email@x" → 给基础分
            if (/@/.test(t)) score += 10;
            if (el.tagName === 'BUTTON') score += 5;
            if (score > bestScore) {
                bestScore = score;
                best = el;
            }
        }
        if (best && bestScore > 0) {
            try {
                best.scrollIntoView({ block: 'center' });
                best.click();
                return {
                    clicked: true,
                    mode: 'scored',
                    score: bestScore,
                    text: (best.textContent || '').slice(0, 80),
                    tag: best.tagName,
                };
            } catch (e) {
                return { clicked: false, error: String(e) };
            }
        }
        // 兜底：找第一个 main 区里的 button（不太严格但通常能命中第一行）
        var first = document.querySelector('main button, [role="main"] button, form button');
        if (first && visible(first)) {
            var ft = (first.textContent || '').replace(/\s+/g, ' ').trim();
            if (!EXCLUDE_RE.test(ft)) {
                try { first.click(); return { clicked: true, mode: 'first_main_btn', text: ft.slice(0, 80) }; } catch (_) { }
            }
        }
        return { clicked: false, mode: 'no_match', candidates: clickables.length };
    };

    // ====== 提交手机号（参考 ref _submit_add_phone_dom） ======
    window.__gpt_pb_submitPhone = function (phone) {
        var parsed = parseDialAndNational(phone);
        var e164 = '+' + String(phone || '').replace(/[^\d]/g, '');
        var natl = parsed.local;
        var iso = parsed.iso;
        var dial = parsed.dial;

        var form = document.querySelector('form[action*="/add-phone" i]');
        if (!form) {
            // 兜底：找 input[type="tel"]
            var input = document.querySelector('input[type="tel"]');
            if (!input) return { ok: false, reason: 'no_form_no_tel_input', url: location.href };
            input.focus();
            setNative(input, natl || e164);
            // 按 Enter
            try {
                input.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true }));
                input.dispatchEvent(new KeyboardEvent('keyup', { key: 'Enter', bubbles: true }));
            } catch (_) { }
            return { ok: true, fallback: 'no_form', url: location.href };
        }

        // 1) channel: SMS（如果有 SMS / WhatsApp 二选一）
        var radios = Array.from(form.querySelectorAll('input[type="radio"]'));
        var entries = radios.map(function (input) {
            var label = input.closest('label');
            var root = label || input.closest('[role="radio"], [data-state]') || input;
            var text = normalize([input.value, label && label.textContent, root && root.textContent].filter(Boolean).join(' '));
            var channel = /\b(sms|text message)\b/i.test(text) || /^sms$/i.test(input.value || '')
                ? 'sms'
                : (/whats\s*app/i.test(text) || /^whatsapp$/i.test(input.value || '') ? 'whatsapp' : '');
            return { input: input, label: label, root: root, channel: channel, text: text };
        });
        var sms = entries.find(function (e) { return e.channel === 'sms'; });
        if (sms) {
            var target = sms.label || sms.root || sms.input;
            try { target.click(); } catch (_) { }
            entries.forEach(function (e) {
                e.input.checked = e.input === sms.input;
                dispatchIO(e.input);
                e.label && e.label.setAttribute && e.label.setAttribute('data-state', e.input === sms.input ? 'on' : 'off');
                e.root && e.root.setAttribute && e.root.setAttribute('data-state', e.input === sms.input ? 'on' : 'off');
            });
            var channelInput = form.querySelector('input[name="channel"]');
            if (channelInput) { channelInput.value = 'sms'; dispatchIO(channelInput); }
        }

        // 2) country select
        var select = form.querySelector('select');
        if (select) {
            var options = Array.from(select.options || []);
            var match = options.find(function (o) {
                return String(o.value || '').toUpperCase() === iso;
            }) || options.find(function (o) {
                return normalize(o.textContent).indexOf('+' + dial) >= 0;
            });
            if (match) {
                select.value = match.value;
                dispatchIO(select);
            }
        }

        // 3) phone input
        var phoneInput = form.querySelector('input[type="tel"], input[autocomplete="tel"]');
        if (!phoneInput) return { ok: false, reason: 'missing_phone_input', url: location.href };
        phoneInput.focus();
        setNative(phoneInput, natl || e164);
        try { phoneInput.dispatchEvent(new Event('blur', { bubbles: true })); } catch (_) { }

        // 4) 同步隐藏 input[name=phoneNumber]
        var hidden = form.querySelector('input[name="phoneNumber"]');
        if (hidden) setNative(hidden, e164);

        // 5) 点 submit
        // 重要：querySelectorAll 是 DOM 顺序，不按 CSS 选择器分组优先级。
        // 这个表单里有 [国家下拉, SMS 单选, WhatsApp 单选, 继续] 多个 button，
        // 必须显式优先 type=submit / 文本匹配 "继续/Continue"，不能 .find 第一个 visible 的。
        var submit = form.querySelector('button[type="submit"]');
        if (!submit || !visible(submit) || submit.disabled) {
            submit = form.querySelector('input[type="submit"]');
        }
        if (!submit || !visible(submit) || submit.disabled) {
            // 文本匹配 "继续/Continue/Next/Submit/提交"
            var all = Array.from(form.querySelectorAll('button')).filter(visible);
            var SUBMIT_RE = /^\s*(?:继续|continue|next|submit|提交|发送(?:验证码)?|send(?:\s+code)?)\s*$/i;
            submit = all.find(function (b) {
                if (b.disabled || b.getAttribute('aria-disabled') === 'true') return false;
                return SUBMIT_RE.test((b.textContent || '').replace(/\s+/g, ' ').trim());
            });
        }
        if (!submit) {
            // 兜底：找最后一个 visible+enabled 的按钮（通常是底部 submit 按钮）
            var all2 = Array.from(form.querySelectorAll('button')).filter(function (b) {
                return visible(b) && !b.disabled && b.getAttribute('aria-disabled') !== 'true';
            });
            submit = all2[all2.length - 1];
        }
        if (!submit) return { ok: false, reason: 'missing_submit', url: location.href };
        try {
            submit.scrollIntoView({ block: 'center' });
            submit.click();
        } catch (_) { }
        log('phone submitted: ' + e164 + ' iso=' + iso + ' dial=+' + dial + ' submit_text=' + (submit.textContent || '').slice(0, 30));
        return {
            ok: true,
            url: location.href,
            selectedCountry: select ? select.value : '',
            visibleValue: phoneInput.value || '',
            hiddenValue: hidden ? hidden.value : '',
            submitText: (submit.textContent || '').replace(/\s+/g, ' ').trim().slice(0, 40),
        };
    };

    // ====== 提交 OTP ======
    window.__gpt_pb_submitOtp = function (code) {
        var otp = String(code || '').trim();
        if (!otp) return { ok: false, reason: 'empty_code' };

        // 找 submit button：优先 type=submit / 文本匹配
        function pickSubmit(form) {
            if (!form) form = document;
            var submit = form.querySelector('button[type="submit"]');
            if (submit && visible(submit) && !submit.disabled) return submit;
            var SUBMIT_RE = /^\s*(?:继续|continue|next|submit|提交|verify|确认|验证)\s*$/i;
            var all = Array.from(form.querySelectorAll('button')).filter(visible);
            var hit = all.find(function (b) {
                if (b.disabled || b.getAttribute('aria-disabled') === 'true') return false;
                return SUBMIT_RE.test((b.textContent || '').replace(/\s+/g, ' ').trim());
            });
            if (hit) return hit;
            // 兜底：最后一个 visible+enabled
            var enabled = all.filter(function (b) {
                return !b.disabled && b.getAttribute('aria-disabled') !== 'true';
            });
            return enabled[enabled.length - 1] || null;
        }

        // 单输入框
        var single = document.querySelector(
            'input[autocomplete="one-time-code"], input[name="code"], input[name="otp"], input[type="tel"][maxlength="6"], input[type="text"][maxlength="6"]'
        );
        if (single && visible(single)) {
            try { single.focus(); } catch (_) { }
            setNative(single, otp);
            try {
                single.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true }));
                single.dispatchEvent(new KeyboardEvent('keyup', { key: 'Enter', bubbles: true }));
            } catch (_) { }
            var btn = pickSubmit(single.closest('form'));
            if (btn) {
                try { btn.scrollIntoView({ block: 'center' }); btn.click(); } catch (_) { }
            }
            return { ok: true, mode: 'single', value: otp,
                     submitText: btn ? (btn.textContent || '').slice(0, 40) : '' };
        }

        // 6 个 maxlength=1
        var grid = Array.from(document.querySelectorAll('input[maxlength="1"]')).filter(visible);
        if (grid.length >= 6) {
            for (var i = 0; i < 6; i++) {
                var el = grid[i];
                try { el.focus(); } catch (_) { }
                try {
                    el.dispatchEvent(new KeyboardEvent('keydown', { key: otp[i], bubbles: true }));
                } catch (_) { }
                setNative(el, otp[i]);
                try {
                    el.dispatchEvent(new KeyboardEvent('keyup', { key: otp[i], bubbles: true }));
                } catch (_) { }
            }
            try { grid[5].blur(); } catch (_) { }
            var btn2 = pickSubmit(grid[5].closest('form'));
            if (btn2) {
                try { btn2.scrollIntoView({ block: 'center' }); btn2.click(); } catch (_) { }
            }
            return { ok: true, mode: 'split', value: otp,
                     submitText: btn2 ? (btn2.textContent || '').slice(0, 40) : '' };
        }
        return { ok: false, reason: 'no_otp_input' };
    };

    // ====== 点击 Resend ======
    window.__gpt_pb_resend = function () {
        var nodes = document.querySelectorAll('button, a, [role="button"]');
        var re = /^\s*(?:resend|resend\s*code|重新发送|再送信|コードを再送)\s*$/i;
        for (var i = 0; i < nodes.length; i++) {
            var t = (nodes[i].textContent || '').trim();
            if (visible(nodes[i]) && re.test(t)) {
                try { nodes[i].click(); return { clicked: true, text: t }; } catch (_) { }
            }
        }
        return { clicked: false };
    };

    // ====== debug ======
    window.__gpt_pb_debug = function () {
        var f = document.querySelector('form[action*="/add-phone" i]');
        return {
            url: location.href.slice(0, 200),
            path: location.pathname,
            state: window.__gpt_pb_state(),
            hasForm: !!f,
            hasTelInput: !!document.querySelector('input[type="tel"]'),
            hasOtpInput: !!document.querySelector('input[autocomplete="one-time-code"], input[maxlength="1"]'),
            title: document.title,
        };
    };

    log('phone_bind.js v1 loaded ' + location.href.slice(0, 80));
})();
