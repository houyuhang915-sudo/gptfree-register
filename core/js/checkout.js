/**
 * ChatGPT Plus Checkout（hosted Stripe）页面自动化
 * 参照 GuJumpgate/content/plus-checkout.js 实现
 *
 * 流程：
 *  1. 在 chatgpt.com 调 backend-api 创建 checkout session 拿到长链
 *  2. 浏览器导航到长链（hosted checkout 在 chatgpt.com/checkout/{processor}/{cs_id}）
 *  3. 页面渲染后选 PayPal、填账单、勾选条款、点 Subscribe
 */
(function () {
    'use strict';
    if (window.__gpt_checkout_v4_loaded__) return;
    window.__gpt_checkout_v4_loaded__ = true;

    var log = function (s) { try { console.log('[GPT-CHECKOUT] ' + s); } catch (_) { } };

    function isVisible(el) {
        if (!el) return false;
        var s = window.getComputedStyle(el);
        if (s.display === 'none' || s.visibility === 'hidden' || s.opacity === '0') return false;
        var r = el.getBoundingClientRect();
        return r.width > 0 && r.height > 0;
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
            setter.call(el, value);
        } catch (_) { el.value = value; }
        el.dispatchEvent(new Event('input', { bubbles: true }));
        el.dispatchEvent(new Event('change', { bubbles: true }));
        el.dispatchEvent(new Event('blur', { bubbles: true }));
        return true;
    }

    function fillByIdSelector(idSel, value) {
        var el = document.getElementById(idSel) || document.querySelector(idSel);
        if (el && isVisible(el)) {
            fillInput(el, value || '');
            return true;
        }
        return false;
    }

    function selectByIdText(id, text) {
        var sel = document.getElementById(id);
        if (!sel) return false;
        text = String(text || '').toLowerCase();
        for (var i = 0; i < sel.options.length; i++) {
            var opt = sel.options[i];
            if (opt.text.toLowerCase().includes(text) || opt.value.toLowerCase().includes(text)) {
                sel.value = opt.value;
                sel.dispatchEvent(new Event('change', { bubbles: true }));
                return true;
            }
        }
        return false;
    }

    function dispatchClick(el) {
        if (!el) return false;
        var r = el.getBoundingClientRect();
        var x = r.left + r.width / 2, y = r.top + r.height / 2;
        ['pointerdown', 'mousedown', 'pointerup', 'mouseup', 'click'].forEach(function (t) {
            try {
                var EvCtor = t.indexOf('pointer') === 0 ? PointerEvent : MouseEvent;
                el.dispatchEvent(new EvCtor(t, {
                    bubbles: true, cancelable: true, view: window,
                    clientX: x, clientY: y, button: 0,
                    pointerId: 1, pointerType: 'mouse'
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
            if (!isVisible(el)) continue;
            if (el.disabled || el.getAttribute('aria-disabled') === 'true') continue;
            var t = getText(el);
            for (var j = 0; j < patterns.length; j++) {
                if (patterns[j].test(t)) return el;
            }
        }
        return null;
    }

    // ====== checkout session 创建（在 chatgpt.com 上） ======
    // 参考 GuJumpgate/services/checkout-converter/app.py 的 PAYMENT_METHOD_CONFIGS
    var PAY_OPENAI_URL_RE = /^https:\/\/(?:pay\.openai\.com|checkout\.stripe\.com)\/c\/pay\//i;

    function findHostedCheckoutUrl(payload) {
        var stack = [payload];
        while (stack.length) {
            var current = stack.shift();
            if (Array.isArray(current)) { stack.push.apply(stack, current); continue; }
            if (!current || typeof current !== 'object') continue;
            for (var k in current) {
                var v = current[k];
                if (typeof v === 'string' && PAY_OPENAI_URL_RE.test(v.trim())) {
                    return v.trim();
                }
                if (v && typeof v === 'object') stack.push(v);
            }
        }
        return '';
    }

    window.__gpt_createCheckout = function (accessToken, planName, country, currency, paymentMethod) {
        planName = planName || 'chatgptplusplan';
        // 支付方式 ↔ checkout_ui_mode / 默认 country/currency / processor 路径
        //   paypal        US / USD / hosted   processor=openai_ie  → pay.openai.com 长链
        //   paypal_custom US / USD / custom   processor=openai_ie  → chatgpt.com/checkout/openai_ie/cs_live_xxx 短链
        //   gopay         ID / IDR / custom   processor=openai_llc → chatgpt.com/checkout/openai_llc/cs_live_xxx 短链
        // 参考 reference/Gpt-Agreement-Payment/FlowPilot/content/plus-checkout.js 的 PAYMENT_METHOD_CONFIGS
        paymentMethod = (paymentMethod || 'paypal').toLowerCase();
        if (paymentMethod === 'paypal-hosted' || paymentMethod === 'paypal_hosted') {
            paymentMethod = 'paypal';
        } else if (paymentMethod === 'paypal-custom' || paymentMethod === 'paypal_us_custom') {
            paymentMethod = 'paypal_custom';
        }
        if (paymentMethod === 'paypal' || paymentMethod === 'paypal_custom') {
            country = country || 'US';
            // 币种留空时按国家推断（JP→JPY，其他默认 USD）
            if (!currency) {
                currency = (String(country).toUpperCase() === 'JP') ? 'JPY' : 'USD';
            }
        } else if (paymentMethod === 'gopay') {
            country = country || 'ID';
            currency = currency || 'IDR';
        } else {
            country = country || 'US';
            currency = currency || 'USD';
        }
        var uiMode = paymentMethod === 'paypal' ? 'hosted' : 'custom';
        log('创建 checkout: plan=' + planName + ' country=' + country + ' currency=' + currency + ' ui=' + uiMode + ' method=' + paymentMethod);

        return fetch('https://chatgpt.com/backend-api/payments/checkout', {
            method: 'POST',
            credentials: 'include',
            headers: {
                'Authorization': 'Bearer ' + accessToken,
                'Content-Type': 'application/json',
                'Accept': 'application/json',
            },
            body: JSON.stringify({
                entry_point: 'all_plans_pricing_modal',
                plan_name: planName,
                checkout_ui_mode: uiMode,
                promo_campaign: {
                    promo_campaign_id: 'plus-1-month-free',
                    is_coupon_from_query_param: false,
                },
                billing_details: { country: country, currency: currency },
            }),
        }).then(function (r) { return r.json(); }).then(function (data) {
            // OpenAI 后端返回错误时，data 里有 error/message/detail 字段，没有 session_id
            // 常见错误：
            //   { detail: "..." }
            //   { error: { code, message } }
            //   { message: "..." }
            //   { error: "string" }
            var sessionId = data.checkout_session_id || data.session_id || '';
            if (!sessionId) {
                var pool = [data.checkout_url || '', data.url || '', data.client_secret || '', data.success_url || ''];
                for (var i = 0; i < pool.length; i++) {
                    var m = (pool[i] || '').match(/cs_(?:live|test)_[A-Za-z0-9]+/);
                    if (m) { sessionId = m[0]; break; }
                }
            }
            // 没拿到 session_id 时，提取错误信息
            if (!sessionId) {
                var errMsg = '';
                if (data.detail) errMsg = (typeof data.detail === 'string') ? data.detail : JSON.stringify(data.detail);
                else if (data.error) {
                    if (typeof data.error === 'string') errMsg = data.error;
                    else if (data.error.message) errMsg = data.error.message;
                    else errMsg = JSON.stringify(data.error);
                }
                else if (data.message) errMsg = data.message;
                else errMsg = 'no_session_id raw=' + JSON.stringify(data).slice(0, 300);
                log('checkout no sid, error=' + errMsg);
                return { error: errMsg };
            }
            // PayPal hosted/custom 的 processor 都是 openai_ie，GoPay 是 openai_llc
            var defaultProcessor = (paymentMethod === 'gopay') ? 'openai_llc' : 'openai_ie';
            var processor = data.processor_entity || defaultProcessor;
            var url = data.checkout_url || data.url || '';
            if (!url && sessionId) {
                url = 'https://chatgpt.com/checkout/' + processor + '/' + sessionId;
            }
            var hostedCheckoutUrl = findHostedCheckoutUrl(data);
            log('checkout: sid=' + sessionId.slice(0, 30) + ' processor=' + processor
                + ' hosted=' + hostedCheckoutUrl.slice(0, 60));
            return {
                sessionId: sessionId,
                processor: processor,
                url: url,
                hostedCheckoutUrl: hostedCheckoutUrl,
                paymentMethod: paymentMethod,
                raw: data,
            };
        }).catch(function (e) {
            log('checkout fetch err: ' + (e && e.message));
            return { error: String(e && e.message || e) };
        });
    };

    // ====== 在 ChatGPT 拿 access_token ======
    window.__gpt_getAccessToken = function () {
        return fetch('/api/auth/session', { credentials: 'include' })
            .then(function (r) { return r.json(); })
            .then(function (data) { return data.accessToken || ''; })
            .catch(function () { return ''; });
    };

    // ====== Hosted Stripe Checkout 的页面自动化 ======

    function isHostedCheckoutPage() {
        var host = (location.hostname || '').toLowerCase();
        var path = (location.pathname || '');
        if (/chatgpt\.com|pay\.openai\.com/.test(host) && /\/checkout|\/c\/pay\//.test(path)) return true;
        if (/checkout\.stripe\.com/.test(host)) return true;
        // 关键 DOM 标志
        if (document.querySelector('#billingAddressLine1')) return true;
        if (document.querySelector('#termsOfServiceConsentCheckbox')) return true;
        // 新版 hosted UI: pay.openai.com /c/pay/cs_live 上有"支付方式"标题、PayPal/银行卡 radio
        if (document.querySelector('input[type="radio"][value="paypal"]')) return true;
        if (document.querySelector('[data-testid="payment-method-paypal"], [data-testid*="paypal"]')) return true;
        return false;
    }

    // ====== 通用 PayPal radio 查找（参照 GuJumpgate findPaymentMethodTarget） ======
    function getCombinedSearchText(el) {
        if (!el) return '';
        var parts = [
            el.textContent, el.value,
            el.getAttribute && el.getAttribute('aria-label'),
            el.getAttribute && el.getAttribute('title'),
            el.getAttribute && el.getAttribute('data-testid'),
            el.getAttribute && el.getAttribute('alt'),
        ];
        return parts.filter(Boolean).join(' ').replace(/\s+/g, ' ').trim();
    }

    function findInteractiveAncestor(el, maxDepth) {
        maxDepth = maxDepth || 6;
        var current = el;
        for (var i = 0; current && i < maxDepth; i++, current = current.parentElement) {
            var tag = (current.tagName || '').toLowerCase();
            if (['button', 'a', 'label'].indexOf(tag) >= 0) return current;
            var role = current.getAttribute && current.getAttribute('role');
            if (role && ['button', 'radio', 'tab', 'link'].indexOf(role) >= 0) return current;
            if (current.tagName === 'INPUT' && /radio|button|submit/i.test(current.type || '')) return current;
            // div 但带可点击的 cursor 或 click 事件
            var style = window.getComputedStyle(current);
            if (style && style.cursor === 'pointer' && current.children.length < 5) return current;
        }
        return null;
    }

    function findPayPalRow() {
        // 1) OpenAI hosted UI (pay.openai.com) 的精准选择器
        var input = document.getElementById('payment-method-accordion-item-title-paypal');
        if (input) {
            // 必须返回 label 否则 React 不响应（input 自己被 hidden 覆盖）
            var label = input.closest('label');
            if (label) return label;
            // input 父级是 .PaymentMethodFormAccordionItemTitle 之类
            var p = input.parentElement;
            for (var i = 0; i < 4 && p; i++, p = p.parentElement) {
                if (p.tagName === 'LABEL') return p;
                if ((p.className || '').toString().indexOf('PaymentMethodFormAccordionItemTitle') >= 0) return p;
            }
            return input;
        }

        // 2) 老 hosted UI 选择器
        var direct = document.querySelector('[data-testid="paypal-accordion-item-button"]')
            || document.querySelector('.paypal-accordion-item button')
            || document.querySelector('[data-testid="paypal-accordion-item"]');
        if (direct && isVisible(direct)) {
            // testid=paypal-accordion-item 是个 div，要点它内部的 label
            var inner = direct.querySelector('label, button, [role="radio"]') || direct;
            return inner;
        }

        // 3) input[type=radio] value/name 含 paypal
        var paypalPattern = /paypal/i;
        var radios = document.querySelectorAll('input[type="radio"], [role="radio"]');
        for (var j = 0; j < radios.length; j++) {
            var r = radios[j];
            if (!isVisible(r) && r.tagName === 'INPUT') {
                // input 可能被 css hide，改判断 label 可见性
            }
            var attrText = (r.getAttribute('value') || '') + ' ' + (r.getAttribute('name') || '') + ' ' + (r.id || '');
            if (paypalPattern.test(attrText)) {
                var lbl = r.closest('label');
                if (lbl && isVisible(lbl)) return lbl;
                return r;
            }
        }

        // 4) 兜底：找含"PayPal"文字的 label / [role=radio]
        var nodes = document.querySelectorAll('label, [role="radio"], button[data-testid]');
        for (var k = 0; k < nodes.length; k++) {
            var el = nodes[k];
            if (!isVisible(el)) continue;
            var t = (el.textContent || '').trim();
            if (paypalPattern.test(t) && t.length < 30) return el;
        }
        return null;
    }

    function isPayPalSelected() {
        var input = document.getElementById('payment-method-accordion-item-title-paypal');
        if (input && input.checked) return true;
        var radios = document.querySelectorAll('input[type="radio"], [role="radio"]');
        for (var i = 0; i < radios.length; i++) {
            var r = radios[i];
            var attrText = (r.getAttribute('value') || '') + ' ' + (r.getAttribute('name') || '') + ' ' + (r.id || '');
            if (!/paypal/i.test(attrText)) continue;
            if (r.checked) return true;
            if (r.getAttribute('aria-checked') === 'true') return true;
            if (r.getAttribute('data-state') === 'checked') return true;
        }
        return false;
    }

    function findCountryDropdown() {
        return document.getElementById('billingCountry')
            || document.querySelector('select[name="country"]')
            || document.querySelector('select[autocomplete="country"]');
    }

    function findSubmitButton() {
        return document.querySelector('button[data-testid="hosted-payment-submit-button"]')
            || document.querySelector('button[data-testid="submit-button"]')
            || document.querySelector('button.SubmitButton--complete')
            || document.querySelector('button[type="submit"]')
            || findClickable([/^subscribe$|^订阅$|^pay$|^支付$|^place\s+order$|start\s+(?:my\s+)?subscription/i]);
    }

    function isSubmitButtonReady(btn) {
        if (!btn) return false;
        if (btn.disabled) return false;
        if (btn.getAttribute && btn.getAttribute('aria-disabled') === 'true') return false;
        var cls = (btn.className && btn.className.toString && btn.className.toString()) || '';
        // SubmitButton--incomplete 表示必填项没填齐，--complete 才能点
        if (/SubmitButton--incomplete/i.test(cls)) return false;
        return true;
    }

    function hideAddressAutocomplete() {
        var styleId = '__gpt_co_hide_autocomplete';
        if (document.getElementById(styleId)) return;
        var style = document.createElement('style');
        style.id = styleId;
        style.textContent = '.AddressAutocomplete-results,[class*="AddressAutocomplete-results"]{display:none!important;height:0!important;overflow:hidden!important}';
        document.head.appendChild(style);
    }

    function dismissCaptchaIfAny() {
        ['#captcha-standalone', '.captcha-overlay', '.captcha-container'].forEach(function (sel) {
            document.querySelectorAll(sel).forEach(function (el) {
                try { el.remove(); } catch (_) { }
            });
        });
    }

    // ====== 状态机（hosted checkout） ======
    window.__gpt_checkout_getStage = function () {
        if (!isHostedCheckoutPage()) return 'not_checkout';
        if (isPayPalSelected()) {
            // PayPal 已选中，看有没有 phone 输入框 / submit 按钮
            return 'paypal_selected';
        }
        var paypalBtn = findPayPalRow();
        var addressInput = document.getElementById('billingAddressLine1');
        var termsBox = document.getElementById('termsOfServiceConsentCheckbox');
        var submitBtn = findSubmitButton();

        if (paypalBtn) return 'select_payment';
        if (addressInput || termsBox || submitBtn) return 'fill_billing';
        return 'loading';
    };

    // 选 PayPal —— 实测点击 button.AccordionButton 才能触发 React state 更新
    window.__gpt_selectPayPal = function () {
        // 1) 真正能触发 React 状态变化的元素：[data-testid="paypal-accordion-item"] 内的 button.AccordionButton
        var dtid = document.querySelector('[data-testid="paypal-accordion-item"]');
        if (dtid) {
            var accBtn = dtid.querySelector('button.AccordionButton');
            if (accBtn) {
                try { accBtn.scrollIntoView({ block: 'center' }); } catch (_) { }
                accBtn.click();
                // 给 React 一点时间响应
                return {
                    clicked: true,
                    via: 'AccordionButton',
                    cls: (accBtn.className || '').toString().slice(0, 80),
                };
            }
        }

        // 2) fallback: 找任何 button class 含 AccordionButton 的，文本含 PayPal
        var btns = document.querySelectorAll('button.AccordionButton');
        for (var i = 0; i < btns.length; i++) {
            var b = btns[i];
            var ctx = (b.closest('[data-testid]') || b.closest('.PaymentMethodFormAccordionItem') || b.parentElement);
            var ctxText = ctx ? (ctx.textContent || '') : '';
            if (/paypal/i.test(ctxText)) {
                b.click();
                return { clicked: true, via: 'AccordionButton_fallback' };
            }
        }

        // 3) 最兜底: 旧版选择器
        var old = document.querySelector('[data-testid="paypal-accordion-item-button"]')
            || document.querySelector('.paypal-accordion-item button');
        if (old) {
            old.click();
            return { clicked: true, via: 'old_selector' };
        }

        return { clicked: false, reason: 'no_target' };
    };

    window.__gpt_isPayPalSelected = function () { return isPayPalSelected(); };

    // 填美国手机号（hosted checkout 上"电话号码"是 Link 用的，可选；填了风控更高）
    window.__gpt_fillPhone = function (phone) {
        if (!phone) return { filled: false, reason: 'empty_phone' };
        var input = document.getElementById('phoneNumber')
            || document.querySelector('input[name="phoneNumber"]')
            || document.querySelector('input[type="tel"]')
            || document.querySelector('input[autocomplete="tel"]');
        if (!input || !isVisible(input)) return { filled: false, reason: 'no_input' };
        // 去掉 +1 前缀（hosted UI 已经显示美国国旗，输入框内只要 10 位）
        var digits = String(phone).replace(/[^\d]/g, '');
        if (digits.length === 11 && digits.charAt(0) === '1') digits = digits.slice(1);
        fillInput(input, digits);
        return { filled: true, value: digits };
    };

    // React 友好的 input 填法（OpenAI hosted UI 用 controlled component）
    function reactFillInput(el, value) {
        if (!el) return false;
        try { el.focus(); } catch (_) { }
        var proto = el.tagName === 'SELECT' ? window.HTMLSelectElement.prototype : window.HTMLInputElement.prototype;
        try {
            var setter = Object.getOwnPropertyDescriptor(proto, 'value').set;
            setter.call(el, String(value));
        } catch (_) {
            el.value = String(value);
        }
        el.dispatchEvent(new Event('input', { bubbles: true }));
        el.dispatchEvent(new Event('change', { bubbles: true }));
        try { el.dispatchEvent(new Event('blur', { bubbles: true })); } catch (_) { }
        return true;
    }

    // 47 都道府県（hosted checkout 选 JP 时 region select 用）
    var JP_PREFECTURES_EN = [
        'Hokkaido','Aomori','Iwate','Miyagi','Akita','Yamagata','Fukushima',
        'Ibaraki','Tochigi','Gunma','Saitama','Chiba','Tokyo','Kanagawa',
        'Niigata','Toyama','Ishikawa','Fukui','Yamanashi','Nagano','Gifu',
        'Shizuoka','Aichi','Mie','Shiga','Kyoto','Osaka','Hyogo','Nara',
        'Wakayama','Tottori','Shimane','Okayama','Hiroshima','Yamaguchi',
        'Tokushima','Kagawa','Ehime','Kochi','Fukuoka','Saga','Nagasaki',
        'Kumamoto','Oita','Miyazaki','Kagoshima','Okinawa'
    ];
    var JP_PREFECTURES_JA = {
        Hokkaido:'北海道',Aomori:'青森県',Iwate:'岩手県',Miyagi:'宮城県',
        Akita:'秋田県',Yamagata:'山形県',Fukushima:'福島県',Ibaraki:'茨城県',
        Tochigi:'栃木県',Gunma:'群馬県',Saitama:'埼玉県',Chiba:'千葉県',
        Tokyo:'東京都',Kanagawa:'神奈川県',Niigata:'新潟県',Toyama:'富山県',
        Ishikawa:'石川県',Fukui:'福井県',Yamanashi:'山梨県',Nagano:'長野県',
        Gifu:'岐阜県',Shizuoka:'静岡県',Aichi:'愛知県',Mie:'三重県',
        Shiga:'滋賀県',Kyoto:'京都府',Osaka:'大阪府',Hyogo:'兵庫県',
        Nara:'奈良県',Wakayama:'和歌山県',Tottori:'鳥取県',Shimane:'島根県',
        Okayama:'岡山県',Hiroshima:'広島県',Yamaguchi:'山口県',Tokushima:'徳島県',
        Kagawa:'香川県',Ehime:'愛媛県',Kochi:'高知県',Fukuoka:'福岡県',
        Saga:'佐賀県',Nagasaki:'長崎県',Kumamoto:'熊本県',Oita:'大分県',
        Miyazaki:'宮崎県',Kagoshima:'鹿児島県',Okinawa:'沖縄県'
    };

    function jpPrefectureCandidates(name) {
        var s = String(name || '').trim();
        var out = [s];
        for (var i = 0; i < JP_PREFECTURES_EN.length; i++) {
            var en = JP_PREFECTURES_EN[i];
            var ja = JP_PREFECTURES_JA[en] || '';
            if (s === en || s.toLowerCase() === en.toLowerCase() || s === ja) {
                out.push(en, ja, en + '-to', en + '-fu', en + '-ken',
                         en + ' Prefecture', en + ' Metropolis');
                break;
            }
        }
        // 去重保序
        var seen = {}, uniq = [];
        for (var j = 0; j < out.length; j++) {
            var v = out[j];
            if (v && !seen[v]) { seen[v] = 1; uniq.push(v); }
        }
        return uniq;
    }

    // 选 region select：先按 candidates 数组里任意一个匹配 option text/value
    function selectRegionByCandidates(selectEl, candidates) {
        if (!selectEl || !selectEl.options) return false;
        for (var i = 0; i < selectEl.options.length; i++) {
            var opt = selectEl.options[i];
            var optText = String(opt.text || '').trim();
            var optVal = String(opt.value || '').trim();
            for (var j = 0; j < candidates.length; j++) {
                var c = String(candidates[j] || '').trim();
                if (!c) continue;
                if (optVal === c || optText === c
                    || optVal.toLowerCase() === c.toLowerCase()
                    || optText.toLowerCase() === c.toLowerCase()
                    || (c.length > 2 && optText.toLowerCase().indexOf(c.toLowerCase()) >= 0)) {
                    reactFillInput(selectEl, opt.value);
                    return opt.value;
                }
            }
        }
        return false;
    }

    // 填账单地址（hosted UI: pay.openai.com）
    // country: 'US'（默认）/ 'JP' / ...
    window.__gpt_fillBillingAddress = function (addr, country) {
        addr = addr || {};
        country = String(country || 'US').toUpperCase();
        var street = addr.street || (country === 'JP' ? 'Marunouchi 1-1' : '123 Main St');
        var city = addr.city || (country === 'JP' ? 'Chiyoda-ku' : 'New York');
        var state = addr.state || (country === 'JP' ? 'Tokyo' : 'New York');
        var zip = addr.zip || (country === 'JP' ? '100-0005' : '10001');

        hideAddressAutocomplete();
        dismissCaptchaIfAny();

        // 1) 设国家
        var countryEl = document.getElementById('billingCountry')
            || document.querySelector('select[name="country"]')
            || document.querySelector('select[autocomplete="country"]');
        if (countryEl && countryEl.value !== country) {
            reactFillInput(countryEl, country);
        }

        // 2) 点 "手动输入地址" 把所有字段展开（多语言：英 / 中 / 日）
        var manual = null;
        var btns = document.querySelectorAll('button, a, [role="button"], span');
        for (var i = 0; i < btns.length; i++) {
            var n = btns[i];
            if (!isVisible(n)) continue;
            var t = (n.textContent || '').trim();
            if (t === '手动输入地址'
                || /^enter address manually$/i.test(t)
                || /住所を手動で入力|手動で入力|手動入力/.test(t)) {
                manual = n;
                break;
            }
        }
        var clickedManual = false;
        if (manual) {
            manual.click();
            clickedManual = true;
        }

        // 3) 给 DOM 一点时间渲染（同步函数没法 await，调用方循环里 sleep）
        // 立即填能填的字段
        var line1 = document.getElementById('billingAddressLine1');
        var loc = document.getElementById('billingLocality');
        var pc = document.getElementById('billingPostalCode');
        var area = document.getElementById('billingAdministrativeArea');

        var filled = {};
        if (line1) { reactFillInput(line1, street); filled.line1 = true; }
        if (loc) { reactFillInput(loc, city); filled.city = true; }
        if (pc) { reactFillInput(pc, zip); filled.zip = true; }

        // 州/省/都道府県
        if (area) {
            var candidates;
            if (country === 'JP') {
                candidates = jpPrefectureCandidates(state);
            } else if (country === 'US') {
                var stateMap = {
                    "AL":"Alabama","AK":"Alaska","AZ":"Arizona","AR":"Arkansas","CA":"California",
                    "CO":"Colorado","CT":"Connecticut","DE":"Delaware","FL":"Florida","GA":"Georgia",
                    "HI":"Hawaii","ID":"Idaho","IL":"Illinois","IN":"Indiana","IA":"Iowa",
                    "KS":"Kansas","KY":"Kentucky","LA":"Louisiana","ME":"Maine","MD":"Maryland",
                    "MA":"Massachusetts","MI":"Michigan","MN":"Minnesota","MS":"Mississippi","MO":"Missouri",
                    "MT":"Montana","NE":"Nebraska","NV":"Nevada","NH":"New Hampshire","NJ":"New Jersey",
                    "NM":"New Mexico","NY":"New York","NC":"North Carolina","ND":"North Dakota","OH":"Ohio",
                    "OK":"Oklahoma","OR":"Oregon","PA":"Pennsylvania","RI":"Rhode Island","SC":"South Carolina",
                    "SD":"South Dakota","TN":"Tennessee","TX":"Texas","UT":"Utah","VT":"Vermont",
                    "VA":"Virginia","WA":"Washington","WV":"West Virginia","WI":"Wisconsin","WY":"Wyoming"
                };
                candidates = [state, stateMap[state] || ''];
                for (var k in stateMap) {
                    if (stateMap[k].toLowerCase() === String(state || '').toLowerCase()) candidates.push(k);
                }
            } else {
                candidates = [state];
            }
            var matched = selectRegionByCandidates(area, candidates);
            if (matched) filled.state = matched;
        }

        // 4) 服务条款已默认勾上，但保险起见再触发一次
        var terms = document.getElementById('termsOfServiceConsentCheckbox');
        if (terms && !terms.checked) {
            terms.click();
        }

        return {
            filled: true,
            country: country,
            clickedManual: clickedManual,
            fields: filled,
            hasLine1: !!line1,
            hasArea: !!area,
        };
    };

    // 勾选服务条款
    window.__gpt_checkTerms = function () {
        var box = document.getElementById('termsOfServiceConsentCheckbox');
        if (box && !box.checked) {
            try { box.click(); } catch (_) { }
        }
        return { checked: !!(box && box.checked) };
    };

    // 点提交
    window.__gpt_clickSubscribe = function () {
        // Auto-completion overlay cleanup
        for (var i = 0; i < 6; i++) hideAddressAutocomplete();
        dismissCaptchaIfAny();

        var btn = findSubmitButton();
        if (!btn) return { clicked: false, reason: 'submit_not_found' };
        if (!isSubmitButtonReady(btn)) {
            return {
                clicked: false,
                reason: 'submit_disabled',
                cls: (btn.className && btn.className.toString()) || '',
                text: getText(btn).slice(0, 60),
            };
        }
        dispatchClick(btn);
        return { clicked: true, text: getText(btn).slice(0, 60) };
    };

    // 检查 hosted checkout 上是否出现"信用卡 verification" 弹窗（OpenAI 端）
    window.__gpt_hasHostedVerification = function () {
        if (document.getElementById('hosted-verification-input')) return true;
        return Boolean(document.querySelector('[data-testid="hosted-verification-modal"]'))
            || Boolean(document.querySelector('input[autocomplete="one-time-code"]'));
    };

    // 检测支付结果（OpenAI 端跳转 success 页）
    window.__gpt_checkPaymentResult = function () {
        var url = location.href || '';
        if (/payments\/success|\/success(?:[/?#]|$)|thank/.test(url)) return 'success';
        if (/paypal\.com/.test(location.hostname || '')) return 'paypal_redirect';
        if (/chatgpt\.com|chat\.openai\.com/.test(location.hostname || '')
            && !/checkout/.test(location.pathname || '')) return 'chatgpt_redirect';
        return 'pending';
    };

    // ============================================================
    // ====== Custom UI checkout（chatgpt.com/checkout/openai_ie/...）
    // ============================================================
    //
    // payment_method=paypal_custom 时用，跑在 chatgpt.com 上：
    //   - 没有 pay.openai.com hosted 长链
    //   - PayPal 不再是 accordion 而是页内 radio（OpenAI 自家 React）
    //   - Subscribe 按钮不一定有 hosted-payment-submit-button testid，要兜底找
    //   - 字段 ID 大多复用 hosted（billingCountry / billingAddressLine1 等）

    function isCustomCheckoutPage() {
        var host = (location.hostname || '').toLowerCase();
        var path = (location.pathname || '');
        if (!/chatgpt\.com|chat\.openai\.com/.test(host)) return false;
        // /checkout/openai_ie/cs_live_xxx 或 /checkout/openai_llc/cs_live_xxx
        return /^\/checkout\/(?:openai_ie|openai_llc|openai_pte)\/cs_(?:live|test)_/.test(path);
    }

    // ============================================================
    // 复用 GuJumpgate findPaymentMethodTarget 算法
    // 核心思路：广撒网搜「可见元素」的「全部文本特征」（textContent、aria-label、
    // alt、src、href、class、dataset、role、data-testid 等），按 /paypal/i 匹配，
    // 然后从命中的元素往父级找最近的可点击 ancestor（button/a/label/role=tab/[tabindex]）。
    // 这套对 OpenAI 自家 React + 任何品牌 SVG/图片标签都鲁棒。
    // ============================================================

    function _ck_normalizeText(t) {
        return String(t || '').replace(/\s+/g, ' ').trim();
    }

    function _ck_getActionText(el) {
        if (!el) return '';
        var attrs = ['aria-label','aria-labelledby','title','placeholder','name','autocomplete',
                     'data-elements-stable-field-name','data-field','data-field-name'];
        var parts = [el.textContent, el.value];
        for (var i = 0; i < attrs.length; i++) {
            if (el.getAttribute) parts.push(el.getAttribute(attrs[i]));
        }
        parts.push(el.id);
        return _ck_normalizeText(parts.filter(Boolean).join(' '));
    }

    function _ck_getSearchText(el) {
        if (!el) return '';
        var ds = [];
        try { if (el.dataset) ds = Object.values(el.dataset); } catch (_) {}
        var attrs = ['alt','role','data-testid','src','href','xlink:href'];
        var parts = [_ck_getActionText(el)];
        for (var i = 0; i < attrs.length; i++) {
            if (el.getAttribute) parts.push(el.getAttribute(attrs[i]));
        }
        parts.push(typeof el.className === 'string' ? el.className : (el.getAttribute && el.getAttribute('class')));
        return _ck_normalizeText(parts.filter(Boolean).concat(ds).join(' '));
    }

    function _ck_getVisibleAll(selector) {
        var nodes = document.querySelectorAll(selector);
        var out = [];
        for (var i = 0; i < nodes.length; i++) {
            if (isVisible(nodes[i])) out.push(nodes[i]);
        }
        return out;
    }

    function _ck_findInteractiveAncestor(el) {
        var current = el;
        for (var d = 0; current && d < 8; d++, current = current.parentElement) {
            if (!isVisible(current)) continue;
            if (current === document.documentElement || current === document.body) break;
            if (['HTML','BODY','MAIN'].indexOf(current.tagName) >= 0) break;
            if (current.matches && current.matches(
                'button, a, label, [role="button"], [role="radio"], [role="tab"], '
                + 'input[type="radio"], input[type="checkbox"], [tabindex]'
            )) {
                return current;
            }
        }
        return null;
    }

    function _ck_isDocumentContainer(el) {
        return !el || el === document.documentElement || el === document.body
            || ['HTML','BODY','MAIN'].indexOf(el.tagName) >= 0;
    }

    function _ck_findClickableByText(patterns) {
        var pats = (Array.isArray(patterns) ? patterns : [patterns]).filter(Boolean);
        var nodes = _ck_getVisibleAll(
            'button, a, [role="button"], [role="tab"], input[type="button"], input[type="submit"], [tabindex]'
        );
        for (var i = 0; i < nodes.length; i++) {
            var t = _ck_getSearchText(nodes[i]);
            for (var j = 0; j < pats.length; j++) {
                if (pats[j].test(t)) return nodes[i];
            }
        }
        return null;
    }

    function _ck_paymentSearchCandidates(pattern) {
        var sel = [
            'button','a','label',
            '[role="button"]','[role="radio"]','[role="tab"]',
            'input[type="radio"]','[tabindex]','[data-testid]',
            '[aria-label]','[title]','img','svg','span','div'
        ].join(', ');
        var hits = [];
        var nodes = _ck_getVisibleAll(sel);
        for (var i = 0; i < nodes.length; i++) {
            var n = nodes[i];
            if (_ck_isDocumentContainer(n)) continue;
            var text = _ck_getSearchText(n);
            if (pattern.test(text)) hits.push(n);
        }
        // 按面积升序：优先小元素（按钮/图标/label），避开大容器
        hits.sort(function (a, b) {
            var ra = a.getBoundingClientRect();
            var rb = b.getBoundingClientRect();
            return (ra.width * ra.height) - (rb.width * rb.height);
        });
        return hits;
    }

    function _ck_findPaymentMethodTarget(pattern) {
        // 1) 直接是可点的元素
        var direct = _ck_findClickableByText([pattern]);
        if (direct) return direct;
        // 2) radio
        var radios = _ck_getVisibleAll('input[type="radio"], [role="radio"]');
        for (var i = 0; i < radios.length; i++) {
            if (pattern.test(_ck_getSearchText(radios[i]))) {
                var lbl = radios[i].closest('label');
                if (lbl && isVisible(lbl)) return lbl;
                return radios[i];
            }
        }
        // 3) 广撒网 → 往上找可点 ancestor
        var cands = _ck_paymentSearchCandidates(pattern);
        for (var c = 0; c < cands.length; c++) {
            var anc = _ck_findInteractiveAncestor(cands[c]);
            if (anc && pattern.test(_ck_getSearchText(anc))) return anc;
        }
        // 4) 兜底：第一个命中元素本身（让 Click 事件冒泡）
        return cands[0] || null;
    }

    function _ck_hasSelectionMarker(el) {
        if (!el) return false;
        if (el.checked) return true;
        if (el.getAttribute && el.getAttribute('aria-checked') === 'true') return true;
        if (el.getAttribute && el.getAttribute('aria-selected') === 'true') return true;
        var ds = el.getAttribute && el.getAttribute('data-state');
        if (ds === 'checked' || ds === 'active' || ds === 'selected') return true;
        var cls = ((el.className || '') + '').toLowerCase();
        return /(?:^|\s|--)(?:selected|active|checked)(?:\s|$|--)/.test(cls);
    }

    function _ck_isPaymentMethodActive(pattern) {
        var cands = _ck_paymentSearchCandidates(pattern);
        for (var i = 0; i < cands.length; i++) {
            var current = cands[i];
            for (var d = 0; current && d < 6; d++, current = current.parentElement) {
                if (_ck_isDocumentContainer(current)) break;
                if (pattern.test(_ck_getSearchText(current)) && _ck_hasSelectionMarker(current)) {
                    return true;
                }
                var radio = current.querySelector && current.querySelector('input[type="radio"], [role="radio"]');
                if (radio
                    && pattern.test(_ck_getSearchText(current) || _ck_getSearchText(radio))
                    && _ck_hasSelectionMarker(radio)) {
                    return true;
                }
            }
        }
        return false;
    }

    var _CK_PAYPAL_PATTERN = /paypal/i;

    function findCustomPaypalRadio() {
        return _ck_findPaymentMethodTarget(_CK_PAYPAL_PATTERN);
    }

    function isCustomPaypalSelected() {
        // 关键判定：主页面正文出现"PayPal が選択されました" / "PayPal selected"
        // 或地址表单（"請求先住所" / "Billing address"）已出现 = 已选 PayPal
        var bodyText = (document.body && document.body.innerText) || '';
        if (/PayPal\s*が\s*選択(?:されました|済み)/i.test(bodyText)) return true;
        if (/PayPal\s+(?:has\s+been\s+)?selected/i.test(bodyText)) return true;
        if (/請求先住所|請求先\s*住所|billing\s*address/i.test(bodyText)) {
            // 出现地址区 → 已经走过支付方式选择
            // 但只在卡字段不可见时才判定（地址区在卡 tab 也可能出现）
            var cardField = document.getElementById('cardNumber')
                || document.querySelector('input[autocomplete="cc-number"]')
                || document.querySelector('input[name="cardnumber"]');
            var cardVisible = cardField && isVisible(cardField);
            if (!cardVisible) return true;
        }

        // 卡字段（cardNumber）可见 → 还在卡 tab，不是 PayPal
        var cardField2 = document.getElementById('cardNumber')
            || document.querySelector('input[autocomplete="cc-number"]')
            || document.querySelector('input[name="cardnumber"]');
        if (cardField2 && isVisible(cardField2)) return false;

        // 走 GuJumpgate 的 hasSelectionMarker 算法
        if (_ck_isPaymentMethodActive(_CK_PAYPAL_PATTERN)) return true;

        return false;
    }

    function findCustomSubscribeButton() {
        // 1) testid / class 精准匹配
        var direct = document.querySelector('button[data-testid="hosted-payment-submit-button"]')
            || document.querySelector('button[data-testid="submit-button"]')
            || document.querySelector('button[data-testid*="subscribe" i]')
            || document.querySelector('button[data-testid*="confirm" i]')
            || document.querySelector('button.SubmitButton--complete')
            || document.querySelector('form button[type="submit"]')
            || document.querySelector('button[type="submit"]');
        if (direct && isVisible(direct)) return direct;

        // 2) 走 GuJumpgate 风格的文本匹配（searchText 包含 aria-label / class / dataset）
        return _ck_findClickableByText([
            /サブスクリプション(?:を)?(?:登録|開始|契約|購入|申(?:し)?込)/i,
            /^subscribe$|^订阅$|开通\s*plus|start\s+(?:my\s+)?subscription/i,
            /^place\s+order$|confirm\s+(?:and\s+)?(?:pay|subscribe)|continue\s+to\s+payment/i,
            /同意.*订阅|确认.*支付|确认.*订阅/i,
            /^購入(?:する)?$|^支払う$|^お支払い$|^続行$/i,
            /(?:プラン|プラス).*(?:契約|購入|登録)/i,
        ]);
    }

    window.__gpt_custom_getStage = function () {
        if (!isCustomCheckoutPage()) {
            // 已离开 custom checkout：可能跳到 paypal.com / success 页
            var host = (location.hostname || '').toLowerCase();
            if (/paypal\.com/.test(host)) return 'paypal_redirect';
            if (/chatgpt\.com|chat\.openai\.com/.test(host)
                && !/\/checkout/.test(location.pathname || '')) return 'left_checkout';
            return 'not_checkout';
        }
        if (isCustomPaypalSelected()) {
            // PayPal 已选 → 看账单表单填了没。
            // OpenAI custom UI 字段："氏名 / 郵便番号 / 都道府県 / 都市名 / 住所(1行目)"
            // 找任意必填字段：name/postal-code/address-level2/address-line1
            var nameEl = document.querySelector('input[autocomplete="name"]');
            var zipEl = document.querySelector('input[autocomplete="postal-code"]');
            var line1El = document.querySelector('input[autocomplete="address-line1"]');
            var cityEl = document.querySelector('input[autocomplete="address-level2"]');
            // hosted UI 的兜底
            var addrEl = document.getElementById('billingAddressLine1');
            var hasForm = nameEl || zipEl || line1El || cityEl || addrEl;
            if (hasForm) {
                // 检查都填了没（任一空 = 还要填）
                var fields = [nameEl, zipEl, line1El, cityEl].filter(Boolean);
                var allFilled = fields.length > 0 && fields.every(function (el) {
                    return el && String(el.value || '').trim();
                });
                if (!allFilled) return 'fill_billing';
            }
            // 表单填完（或没表单），看 submit 是否 ready
            var sub = findCustomSubscribeButton();
            if (sub && !sub.disabled) return 'submit_ready';
            return 'paypal_selected';
        }
        var paypalBtn = findCustomPaypalRadio();
        if (paypalBtn) return 'select_payment';
        return 'loading';
    };

    window.__gpt_custom_selectPayPal = function () {
        var target = findCustomPaypalRadio();
        if (!target) return { clicked: false, reason: 'no_target' };
        try { target.scrollIntoView({ block: 'center' }); } catch (_) { }
        // 先点 1 次（GuJumpgate 实测连点 2 次更稳）
        var clickable = target;
        if (target.tagName === 'INPUT') {
            var lbl = target.closest('label');
            if (lbl && isVisible(lbl)) clickable = lbl;
        }
        dispatchClick(clickable);
        // 200ms 后再点 1 次（如果还没切换）
        try {
            setTimeout(function () {
                if (!isCustomPaypalSelected()) dispatchClick(clickable);
            }, 250);
        } catch (_) { }
        return {
            clicked: true,
            tag: clickable.tagName,
            text: _ck_getActionText(clickable).slice(0, 60),
            cls: ((clickable.className || '') + '').slice(0, 80)
        };
    };

    window.__gpt_custom_isPaypalSelected = function () {
        return isCustomPaypalSelected();
    };

    window.__gpt_custom_clickSubscribe = function () {
        for (var i = 0; i < 4; i++) hideAddressAutocomplete();
        dismissCaptchaIfAny();
        var btn = findCustomSubscribeButton();
        if (!btn) return { clicked: false, reason: 'submit_not_found' };
        if (btn.disabled || btn.getAttribute('aria-disabled') === 'true') {
            return {
                clicked: false,
                reason: 'submit_disabled',
                cls: ((btn.className || '') + '').slice(0, 80),
                text: getText(btn).slice(0, 60)
            };
        }
        try { btn.scrollIntoView({ block: 'center' }); } catch (_) { }
        dispatchClick(btn);
        return { clicked: true, text: getText(btn).slice(0, 60) };
    };

    // 调试用：把 PayPal/Subscribe 的查找结果摘要返回，便于排查为什么 stage=loading
    window.__gpt_custom_debug = function () {
        var pp = findCustomPaypalRadio();
        var sb = findCustomSubscribeButton();
        var cands = _ck_paymentSearchCandidates(_CK_PAYPAL_PATTERN);
        // 收集页内 button 文本（前 30 个）
        var btnTexts = [];
        document.querySelectorAll('button').forEach(function (b) {
            if (!isVisible(b)) return;
            var t = _ck_getActionText(b);
            if (t && btnTexts.length < 30) btnTexts.push(t.slice(0, 60));
        });
        return {
            url: location.href.slice(0, 140),
            host: location.hostname,
            isCustom: isCustomCheckoutPage(),
            paypalSelected: isCustomPaypalSelected(),
            paypalCandidates: cands.slice(0, 5).map(function (c) {
                var rect = c.getBoundingClientRect();
                return {
                    tag: c.tagName,
                    role: c.getAttribute && c.getAttribute('role'),
                    text: _ck_getActionText(c).slice(0, 80),
                    tid: c.getAttribute && c.getAttribute('data-testid'),
                    cls: ((c.className || '') + '').slice(0, 80),
                    rect: Math.round(rect.width) + 'x' + Math.round(rect.height),
                };
            }),
            paypalTarget: pp ? {
                tag: pp.tagName,
                role: pp.getAttribute && pp.getAttribute('role'),
                text: _ck_getActionText(pp).slice(0, 80),
                tid: pp.getAttribute && pp.getAttribute('data-testid'),
                cls: ((pp.className || '') + '').slice(0, 80),
            } : null,
            submitBtn: sb ? {
                text: _ck_getActionText(sb).slice(0, 80),
                tid: sb.getAttribute && sb.getAttribute('data-testid'),
                disabled: sb.disabled,
                cls: ((sb.className || '') + '').slice(0, 80),
            } : null,
            visibleButtons: btnTexts,
            cardFieldVisible: (function () {
                var cf = document.getElementById('cardNumber')
                    || document.querySelector('input[autocomplete="cc-number"]');
                return cf && isVisible(cf);
            })(),
        };
    };

    log('checkout.js v2 注入成功 ' + location.href.slice(0, 80));
})();
