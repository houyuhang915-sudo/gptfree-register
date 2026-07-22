/**
 * PayPal 支付页自动化（参照 GuJumpgate/content/paypal-flow.js）
 *
 * 阶段 (window.__gpt_paypal_getStage 返回值):
 *   - "outside_paypal"            不在 paypal.com
 *   - "verification"              hosted checkout 6 位验证码弹窗 (ci-ciBasic-N)
 *   - "guest_checkout"            游客填卡页 (#cardNumber / #billingLine1)
 *   - "review"                    /webapps/hermes 同意续订（"Set up once. Pay faster..." + #consentButton）
 *   - "login"                     /pay 或有 #email 输入框
 *   - "approval"                  普通账号登录后的同意按钮（findApproveButton）
 *   - "unknown"                   都不命中
 */
(function () {
    'use strict';
    if (window.__gpt_paypal_v2_loaded__) return;
    window.__gpt_paypal_v2_loaded__ = true;

    var log = function (s) { try { console.log('[GPT-PAYPAL] ' + s); } catch (_) { } };

    // ===== 工具 =====
    function isVisible(el) {
        if (!el) return false;
        var s = window.getComputedStyle(el);
        if (s.display === 'none' || s.visibility === 'hidden' || s.opacity === '0') return false;
        var r = el.getBoundingClientRect();
        return r.width > 0 && r.height > 0;
    }
    function isEnabled(el) {
        return Boolean(el) && !el.disabled && el.getAttribute('aria-disabled') !== 'true';
    }
    function getText(el) {
        if (!el) return '';
        return [el.textContent, el.value, el.getAttribute && el.getAttribute('aria-label')]
            .filter(Boolean).join(' ').replace(/\s+/g, ' ').trim();
    }
    function fillInput(el, value) {
        if (!el) return false;
        try { el.focus(); } catch (_) { }
        try {
            var setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
            setter.call(el, String(value || ''));
        } catch (_) { el.value = String(value || ''); }
        el.dispatchEvent(new Event('input', { bubbles: true }));
        el.dispatchEvent(new Event('change', { bubbles: true }));
        el.dispatchEvent(new Event('blur', { bubbles: true }));
        return true;
    }
    function fillById(id, val) {
        var el = document.getElementById(id);
        if (el && isVisible(el) && isEnabled(el)) { fillInput(el, val); return true; }
        return false;
    }
    function findFirstVisible(selectors) {
        for (var i = 0; i < selectors.length; i++) {
            var el = document.querySelector(selectors[i]);
            if (el && isVisible(el) && isEnabled(el)) return el;
        }
        return null;
    }
    function findLabel(el) {
        if (!el) return '';
        if (el.id) {
            try {
                var label = document.querySelector('label[for="' + CSS.escape(el.id) + '"]');
                if (label) return (label.textContent || '').trim();
            } catch (_) { }
        }
        var parentLabel = el.closest && el.closest('label');
        if (parentLabel) return (parentLabel.textContent || '').replace(/\s+/g, ' ').trim();
        return '';
    }
    function stripAccents(s) {
        return String(s || '').toLowerCase().normalize('NFD').replace(/[̀-ͯ]/g, '');
    }
    function elementSearchText(el) {
        var parts = [];
        ['name', 'id', 'autocomplete', 'data-field', 'data-testid', 'aria-label', 'placeholder'].forEach(function (attr) {
            var v = el.getAttribute && el.getAttribute(attr);
            if (v) parts.push(v);
        });
        var label = findLabel(el);
        if (label) parts.push(label);
        var parent = el.closest && el.closest('label, .field, .form-group, .input-group, div');
        if (parent) {
            var labelEl = parent.querySelector && parent.querySelector('label, .label, span');
            if (labelEl) parts.push(labelEl.textContent || '');
        }
        return stripAccents(parts.join(' '));
    }
    function matchesLogicalField(fieldKey, text) {
        var t = stripAccents(text);
        var negCard = /card|cartao|cc-number|cvv|cvc/;
        if (fieldKey === 'email') return /\b(e-?mail|email)\b/.test(t);
        if (fieldKey === 'phone') return /\b(phone|mobile|cell|tel|telefone|celular)\b/.test(t) && !/cpf|document|doc|card|cartao/.test(t);
        if (fieldKey === 'firstName') return /(first.?name|given.?name|primeiro nome)/.test(t) && !/full|completo|last|family|sobrenome/.test(t);
        if (fieldKey === 'lastName') return /(last.?name|family.?name|surname|sobrenome)/.test(t);
        if (fieldKey === 'billingLine1') return /(address.?line.?1|street|endereco|logradouro|rua)/.test(t) && !/city|cidade|state|estado|cep|postal|zip|numero|number/.test(t);
        if (fieldKey === 'billingNumber') return /(numero|nº|n°|house.*number|address.*number|billing.*number)/.test(t) && !/phone|telefone|cpf|document|doc/.test(t) && !negCard.test(t);
        if (fieldKey === 'billingNeighborhood') return /(bairro|district|distrito|neighbou?rhood)/.test(t);
        if (fieldKey === 'billingCity') return /(city|cidade|municipio)/.test(t);
        if (fieldKey === 'billingPostalCode') return /(cep|postal.?code|codigo.*postal|zip|postal)/.test(t);
        if (fieldKey === 'dateOfBirth') return /(date.*birth|birth.*date|birthday|dob|bday|nascimento|data.*nascimento)/.test(t);
        if (fieldKey === 'taxId') return /(cpf|documento|document|tax.?id|taxid|national|identity)/.test(t);
        return false;
    }
    function findFieldByText(fieldKey) {
        if (!fieldKey) return null;
        var fields = document.querySelectorAll('input, textarea');
        for (var i = 0; i < fields.length; i++) {
            var el = fields[i];
            if (!isVisible(el) || !isEnabled(el) || el.readOnly || el.type === 'hidden') continue;
            if (el.value && String(el.value).trim() && fieldKey !== 'password') continue;
            if (matchesLogicalField(fieldKey, elementSearchText(el))) return el;
        }
        return null;
    }
    function fillAny(id, selectors, val, fieldKey) {
        if (fillById(id, val)) return true;
        var el = findFirstVisible(selectors || []);
        if (!el && fieldKey) {
            el = findFieldByText(fieldKey);
        }
        return fillInput(el, val);
    }
    function selectById(id, text) {
        return selectAny(id, [], [text]);
    }
    function selectAny(id, selectors, candidates) {
        var sels = [document.getElementById(id)].concat((selectors || []).map(function (s) { return document.querySelector(s); })).filter(Boolean);
        var vals = (candidates || []).map(function (v) { return String(v || '').trim(); }).filter(Boolean);
        var normVals = vals.map(stripAccents);
        for (var i = 0; i < sels.length; i++) {
            var sel = sels[i];
            for (var j = 0; j < (sel.options || []).length; j++) {
                var o = sel.options[j];
                var ov = String(o.value || '').trim();
                var ot = String(o.text || '').trim();
                var nov = stripAccents(ov), not = stripAccents(ot);
                for (var k = 0; k < vals.length; k++) {
                    if (ov === vals[k] || ot === vals[k] || nov === normVals[k] || not === normVals[k] || (normVals[k].length > 2 && not.indexOf(normVals[k]) >= 0)) {
                        sel.value = o.value;
                        sel.dispatchEvent(new Event('input', { bubbles: true }));
                        sel.dispatchEvent(new Event('change', { bubbles: true }));
                        return true;
                    }
                }
            }
        }
        return false;
    }
    function selectCountry(code) {
        code = String(code || 'US').toUpperCase();
        var sels = [
            document.getElementById('billingCountry'),
            document.getElementById('country'),
            document.querySelector('select[name="country"]'),
            document.querySelector('select[autocomplete="country"]'),
            document.querySelector('select[id*="country" i]'),
        ].filter(Boolean);
        for (var i = 0; i < sels.length; i++) {
            var sel = sels[i];
            for (var j = 0; j < (sel.options || []).length; j++) {
                var opt = sel.options[j];
                if (String(opt.value || '').toUpperCase() === code) {
                    sel.value = opt.value;
                    sel.dispatchEvent(new Event('input', { bubbles: true }));
                    sel.dispatchEvent(new Event('change', { bubbles: true }));
                    return true;
                }
            }
        }
        return false;
    }
    function normalizePhoneForCountry(phone, country) {
        var digits = String(phone || '').replace(/[^\d]/g, '');
        country = String(country || 'US').toUpperCase();
        if (country === 'US' && digits.length === 11 && digits[0] === '1') return digits.slice(1);
        if (country === 'BR' && digits.indexOf('55') === 0) return digits.slice(2);
        if (country === 'JP' && digits.indexOf('81') === 0) return '0' + digits.slice(2);
        if (country === 'GB') {
            if (digits.indexOf('44') === 0) digits = digits.slice(2);
            if (digits[0] === '0') digits = digits.slice(1);
            return digits;
        }
        return digits;
    }
    function ensureCreditSelected() {
        var rx = /\bcredit\b|cr[eé]dito/i;
        var nodes = document.querySelectorAll('input[type="radio"], [role="radio"], label, button, [role="button"]');
        for (var i = 0; i < nodes.length; i++) {
            var el = nodes[i];
            if (!isVisible(el) || !isEnabled(el)) continue;
            var text = [
                getText(el),
                el.getAttribute && el.getAttribute('value'),
                el.getAttribute && el.getAttribute('name'),
            ].filter(Boolean).join(' ');
            if (!rx.test(text)) continue;
            if (el.checked || el.getAttribute('aria-checked') === 'true') return 'already_credit';
            dispatchClick(el);
            return 'clicked_credit';
        }
        return 'credit_not_found';
    }
    function acceptRequiredTerms() {
        var required = /contrato|usu[aá]rio|declara[cç][aã]o|privacidade|maior de idade|user agreement|privacy/i;
        var promo = /promo[cç][oõ]es|ofertas|marketing|promotions|offers/i;
        var boxes = document.querySelectorAll('input[type="checkbox"], [role="checkbox"]');
        var fallback = null;
        for (var i = 0; i < boxes.length; i++) {
            var b = boxes[i];
            if (!isVisible(b) || !isEnabled(b)) continue;
            if (b.checked || b.getAttribute('aria-checked') === 'true') continue;
            var scope = b.closest('label,div,li,section') || b;
            var text = scope.innerText || scope.textContent || '';
            if (required.test(text)) { dispatchClick(b); return 'clicked_terms'; }
            if (!fallback && !promo.test(text)) fallback = b;
        }
        if (fallback) { dispatchClick(fallback); return 'clicked_first_checkbox'; }
        return 'terms_not_found';
    }
    function dispatchClick(el) {
        if (!el) return false;
        try { el.scrollIntoView({ block: 'center' }); } catch (_) { }
        var r = el.getBoundingClientRect();
        var x = r.left + r.width / 2, y = r.top + r.height / 2;
        ['pointerdown', 'mousedown', 'pointerup', 'mouseup', 'click'].forEach(function (t) {
            try {
                var Ev = t.indexOf('pointer') === 0 ? PointerEvent : MouseEvent;
                el.dispatchEvent(new Ev(t, {
                    bubbles: true, cancelable: true, view: window,
                    clientX: x, clientY: y, button: 0,
                    pointerId: 1, pointerType: 'mouse',
                }));
            } catch (_) { }
        });
        try { el.click(); } catch (_) { }
        return true;
    }
    function findClickable(patterns) {
        var nodes = document.querySelectorAll('button, a, [role="button"], input[type="submit"]');
        for (var i = 0; i < nodes.length; i++) {
            var el = nodes[i];
            if (!isVisible(el) || !isEnabled(el)) continue;
            var t = getText(el);
            if (!t) continue;
            for (var j = 0; j < patterns.length; j++) {
                if (patterns[j].test(t)) return el;
            }
        }
        return null;
    }

    // ===== 检测各阶段 DOM =====

    function findHostedVerificationInputs() {
        // PayPal hosted checkout OTP: #ci-ciBasic-0 .. #ci-ciBasic-5
        var arr = [];
        for (var i = 0; i < 6; i++) {
            var el = document.getElementById('ci-ciBasic-' + i);
            if (el && isVisible(el)) arr.push(el);
        }
        return arr;
    }
    function isVerificationStage() {
        return findHostedVerificationInputs().length >= 6;
    }

    function isGuestCheckoutStage() {
        var p = (location.pathname || '');
        if (/\/checkoutweb\//i.test(p)) return true;
        return Boolean(document.getElementById('cardNumber'))
            || Boolean(document.getElementById('billingLine1'));
    }

    function isReviewStage() {
        return /\/webapps\/hermes/i.test(location.pathname || '');
    }

    function isLoginStage() {
        var p = (location.pathname || '');
        if (p === '/pay') return true;
        return Boolean(document.getElementById('email'));
    }

    function findApproveButton() {
        // PayPal 主要 approve 按钮，FlowPilot 用 textual + data-testid
        var direct = document.querySelector('button[data-testid="submit-button"]')
            || document.querySelector('button#payment-submit-btn')
            || document.querySelector('button[name="agreeAndContinue"]');
        if (direct && isVisible(direct) && isEnabled(direct)) return direct;
        return findClickable([
            /^\s*agree\s*(?:and)?\s*continue\s*$/i,
            /^\s*authorize\s*$/i,
            /^\s*pay\s*now\s*$/i,
            /^\s*同意并继续\s*$/,
            /^\s*授权\s*$/,
            /^\s*同意して(?:続行|続ける|次へ)\s*$/,
            /^\s*同意する\s*$/,
            /^\s*今すぐ(?:支払う|お支払い)\s*$/,
            /^\s*承認(?:する)?\s*$/,
        ]);
    }

    function findReviewConsentButton() {
        var direct = document.getElementById('consentButton')
            || document.querySelector('button[data-testid="consentButton"]');
        if (direct && isVisible(direct) && isEnabled(direct)) return direct;
        return findClickable([
            /agree\s*(?:and)?\s*continue|^continue$/i,
            /同意并继续|^继续$/,
            /^同意して(?:続行|続ける|次へ)$|^同意する$|^次へ$|^続行$|^続ける$/,
        ]);
    }

    window.__gpt_paypal_getStage = function () {
        var host = (location.hostname || '').toLowerCase();
        if (!/paypal\./i.test(host)) return 'outside_paypal';
        if (isVerificationStage()) return 'verification';
        if (isGuestCheckoutStage()) return 'guest_checkout';
        if (isReviewStage() && findReviewConsentButton()) return 'review';
        if (isLoginStage()) return 'login';
        if (findApproveButton()) return 'approval';
        return 'unknown';
    };

    // ===== 移除 captcha overlay =====
    function removeCaptchaOverlay() {
        ['#captcha-standalone', '.captcha-overlay', '.captcha-container'].forEach(function (sel) {
            document.querySelectorAll(sel).forEach(function (el) {
                try { el.remove(); } catch (_) { }
            });
        });
    }

    // ===== 阶段动作 =====

    // /pay 登录页：填邮箱 + 密码（如果配置了 paypal 账号），否则点击 "Create Account" 走 guest
    window.__gpt_paypal_login = function (config) {
        config = config || {};
        var email = config.paypalEmail || '';
        var password = config.paypalPassword || '';

        // 如果没有 paypal 账号，则去找 "Create Account" 切到 guest 路径
        if (!email && !password) {
            var createBtn = document.getElementById('createAccount')
                || document.querySelector('button[data-testid="createAccountButton"]')
                || findClickable([
                    /create\s*(?:an?\s*)?account|没有.*帐户|创建.*帐户/i,
                    /アカウントを開設(?:する)?|アカウント作成|新規(?:登録|作成)/,
                ]);
            if (createBtn) {
                dispatchClick(createBtn);
                return { mode: 'guest', clicked: true };
            }
            // 兜底：直接填随机邮箱 + 点 next
            var fakeEmail = 'guest_' + Math.random().toString(36).slice(2, 14) + '@gmail.com';
            fillById('email', fakeEmail);
            var next = document.querySelector('button[data-testid="next-button"]')
                || findClickable([/next|continue|下一步|继续|^次へ$|^続行$|^つぎへ$/i]);
            if (next) dispatchClick(next);
            return { mode: 'guest', clicked: !!next, email: fakeEmail };
        }

        // 有 paypal 账号：邮箱 → next → 密码 → login
        var emailInput = document.getElementById('email');
        if (emailInput && isVisible(emailInput)) {
            fillInput(emailInput, email);
            var emailNext = document.querySelector('button[data-testid="next-button"]')
                || document.getElementById('btnNext')
                || findClickable([/next|continue|下一步|继续|^次へ$|^続行$|^つぎへ$/i]);
            if (emailNext) {
                dispatchClick(emailNext);
                return { mode: 'login', phase: 'email_submitted' };
            }
        }
        var passwordInput = document.getElementById('password')
            || document.querySelector('input[type="password"]');
        if (passwordInput && isVisible(passwordInput)) {
            fillInput(passwordInput, password);
            var loginBtn = document.querySelector('button[data-testid="login-button"]')
                || document.getElementById('btnLogin')
                || findClickable([/log\s*in|sign\s*in|登录|登入|^ログイン$/i]);
            if (loginBtn) dispatchClick(loginBtn);
            return { mode: 'login', phase: 'password_submitted' };
        }
        return { mode: 'login', phase: 'unknown' };
    };

    // Guest 卡支付页：填邮箱/电话/卡号/密码/姓名/地址，点 Submit
    window.__gpt_paypal_fillGuest = function (cfg) {
        cfg = cfg || {};
        removeCaptchaOverlay();

        var addr = cfg.address || {};
        var targetCountry = String(
            cfg.paypalCountry || cfg.billingCountry || cfg.country || addr.country || 'US'
        ).toUpperCase();
        selectCountry(targetCountry);
        if (targetCountry === 'BR') ensureCreditSelected();

        var email = cfg.email || ('guest_' + Math.random().toString(36).slice(2, 14) + '@gmail.com');
        var phone = normalizePhoneForCountry(cfg.phone || '', targetCountry);
        var pwd = cfg.password || ('Pwd' + Math.random().toString(36).slice(2, 12) + '!Z9');
        var firstName = cfg.firstName || 'James';
        var lastName = cfg.lastName || 'Smith';

        if (!cfg.cardNumber) return { filled: false, reason: 'no_card_number' };

        var cardNum = String(cfg.cardNumber).replace(/\s+/g, '');
        var cardExpiry = cfg.cardExpiry || '';
        var cardCvv = cfg.cardCvv || '';

        var brField = targetCountry === 'BR';
        fillAny('email', ['input[autocomplete="email"]', 'input[type="email"]'], email, brField ? 'email' : '');
        fillAny('phone', ['input[autocomplete="tel"]', 'input[autocomplete="tel-national"]', 'input[type="tel"]'], phone, brField ? 'phone' : '');
        fillById('cardNumber', cardNum);
        fillById('cardExpiry', cardExpiry);
        fillById('cardCvv', cardCvv);
        fillById('password', pwd);
        fillAny('firstName', ['input[autocomplete="given-name"]', 'input[name*="firstName" i]', 'input[data-testid*="first-name" i]'], firstName, brField ? 'firstName' : '');
        fillAny('lastName', ['input[autocomplete="family-name"]', 'input[name*="lastName" i]', 'input[data-testid*="last-name" i]'], lastName, brField ? 'lastName' : '');
        fillAny('billingLine1', ['input[autocomplete="address-line1"]', 'input[autocomplete="street-address"]', 'input[name*="address" i]', 'input[name*="street" i]'], addr.street || '123 Main St', brField ? 'billingLine1' : '');
        if (targetCountry === 'BR') {
            fillAny('billingNumber', [
                'input[name="billingAddressNumber"]',
                'input[id="billingAddressNumber"]',
                'input[name*="addressNumber" i]',
                'input[id*="addressNumber" i]',
                'input[name*="billingNumber" i]',
                'input[id*="billingNumber" i]',
                'input[name*="streetNumber" i]',
                'input[id*="streetNumber" i]',
                'input[aria-label="Nº"]',
                'input[placeholder="Nº"]',
            ], addr.number || addr.streetNumber || '', 'billingNumber');
            fillAny('billingNeighborhood', [
                'input[name*="neighborhood" i]',
                'input[id*="neighborhood" i]',
                'input[name*="district" i]',
                'input[id*="district" i]',
                'input[name*="bairro" i]',
                'input[id*="bairro" i]',
                'input[aria-label*="bairro" i]',
                'input[placeholder*="bairro" i]',
                'input[aria-label*="distrito" i]',
                'input[placeholder*="distrito" i]',
            ], addr.district || addr.neighborhood || '', 'billingNeighborhood');
        }
        fillAny('billingCity', ['input[autocomplete="address-level2"]', 'input[name*="city" i]', 'input[id*="city" i]'], addr.city || 'New York', brField ? 'billingCity' : '');
        fillAny('billingPostalCode', ['input[autocomplete="postal-code"]', 'input[name*="postal" i]', 'input[name*="zip" i]', 'input[name*="cep" i]'], addr.zip || addr.postalCode || '10001', brField ? 'billingPostalCode' : '');
        var stateCandidates = [addr.state || 'New York'];
        if (targetCountry === 'BR' && addr.stateName) stateCandidates.push(addr.stateName, stripAccents(addr.stateName));
        selectAny('billingState', [
            'select[name*="state" i]', 'select[id*="state" i]',
            'select[name*="administrative" i]', 'select[id*="administrative" i]',
            'select[autocomplete="address-level1"]', 'select[data-testid*="state" i]',
            'select[data-testid*="region" i]', 'select[aria-label*="estado" i]'
        ], stateCandidates);
        if (targetCountry === 'BR') {
            fillAny('dateOfBirth', [
                'input[name*="dateOfBirth" i]',
                'input[id*="dateOfBirth" i]',
                'input[data-testid*="dob" i]',
                'input[autocomplete="bday"]',
            ], cfg.dateOfBirth || cfg.dob || '', 'dateOfBirth');
            fillAny('taxId', [
                'input[name*="cpf" i]',
                'input[id*="cpf" i]',
                'input[name*="tax" i]',
                'input[id*="tax" i]',
                'input[name*="document" i]',
                'input[id*="document" i]',
                'input[placeholder*="CPF" i]',
            ], cfg.cpf || cfg.taxId || '', 'taxId');
            acceptRequiredTerms();
        }

        // 提交
        setTimeout(function () {
            removeCaptchaOverlay();
            var submit = document.querySelector('button[data-testid="submit-button"]')
                || document.querySelector('button[data-testid="hosted-payment-submit-button"]')
                || document.querySelector('button.SubmitButton--complete')
                || findClickable([/^pay$|^continue$|^submit$|^next$|^subscribe$|^pagar$|^continuar$|^enviar$|^assinar$|^pr[oó]ximo$|criar conta|concordar.*criar|^支付$|^继续$|^下一步$|^次へ$|^続行$|^購入$|^お支払い$|^今すぐ(?:支払う|お支払い)$/i]);
            if (submit) dispatchClick(submit);
        }, 800);

        return {
            filled: true,
            email: email,
            password: pwd,
            country: targetCountry,
            submitting: true,
        };
    };

    // 填 hosted checkout 6 位 OTP
    window.__gpt_paypal_fillVerification = function (code) {
        code = String(code || '');
        if (!/^\d{6}$/.test(code)) return { filled: false, reason: 'bad_code' };
        var inputs = findHostedVerificationInputs();
        if (inputs.length < 6) return { filled: false, reason: 'no_inputs' };
        var setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
        for (var i = 0; i < 6; i++) {
            var el = inputs[i];
            try { el.focus(); } catch (_) { }
            setter.call(el, code[i]);
            el.dispatchEvent(new InputEvent('input', { bubbles: true, data: code[i], inputType: 'insertText' }));
            el.dispatchEvent(new Event('change', { bubbles: true }));
        }
        try { inputs[5].blur(); } catch (_) { }
        return { filled: true };
    };

    // /webapps/hermes 同意续订
    window.__gpt_paypal_consent = function () {
        var btn = findReviewConsentButton();
        if (!btn) return { clicked: false };
        dispatchClick(btn);
        return { clicked: true, text: getText(btn).slice(0, 60) };
    };

    // 主 approve 按钮
    window.__gpt_paypal_approve = function () {
        // 关掉 passkey 弹窗等
        var dismiss = findClickable([
            /^cancel$|^close$|^not\s*now$|^maybe\s*later$|^skip$|^取消$|^关闭$|^稍后$|^不保存$/i,
            /^キャンセル$|^閉じる$|^後で$|^あとで$|^スキップ$|^保存しない$/,
        ]);
        if (dismiss) {
            dispatchClick(dismiss);
        }
        var btn = findApproveButton();
        if (!btn) return { clicked: false };
        dispatchClick(btn);
        return { clicked: true, text: getText(btn).slice(0, 60) };
    };

    // 调试快照
    window.__gpt_paypal_debug = function () {
        var btns = document.querySelectorAll('button, [role="button"], input[type="submit"]');
        var visible = [];
        for (var i = 0; i < btns.length && visible.length < 12; i++) {
            if (!isVisible(btns[i])) continue;
            var t = getText(btns[i]).slice(0, 60);
            if (t) visible.push(t);
        }
        return {
            host: location.hostname,
            path: location.pathname,
            url: location.href.slice(0, 200),
            stage: window.__gpt_paypal_getStage(),
            buttons: visible,
            hasEmail: !!document.getElementById('email'),
            hasCard: !!document.getElementById('cardNumber'),
            hasConsent: !!document.getElementById('consentButton'),
            hasOTP: isVerificationStage(),
        };
    };

    log('paypal.js v2 注入成功 ' + location.href.slice(0, 80));
})();
