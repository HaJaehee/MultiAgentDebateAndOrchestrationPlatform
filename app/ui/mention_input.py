"""입력창의 @언급 창 (브라우저 쪽).

후보는 서버가 고릅니다 (`app/workspace_files.py`). 목록 전체를 브라우저로 보내지 않기
위해서입니다. 여기서는 커서 앞의 `@조각` 을 찾아 서버에 묻고, 받은 후보를 입력창 위에
띄우고, 키보드로 고르게 합니다.

키 처리는 **문서의 캡처 단계**에서 합니다. 입력창의 Enter 는 이미 "보내기" 에 묶여
있습니다 (`chat_feed.SUBMIT_KEY_EVENT`, Vue 가 입력 요소 자체에 건 핸들러). 창이 열려
있을 때의 Enter 는 **고르기**여야 하므로, 이벤트가 입력 요소에 닿기 전에 가로채 멈춥니다.
후보가 없으면 가로채지 않아 Enter 가 그대로 보내기가 됩니다.

한글 조합 중(`isComposing`)의 Enter·방향키는 건드리지 않습니다. 조합을 확정하는 Enter 가
후보 선택으로 새면 쓰던 글자가 사라집니다.

서버와는 NiceGUI 의 전역 이벤트로 이야기합니다.

* 브라우저 → 서버: `emitEvent('mado_mention_query', {id, seq, query})` — `id` 는 입력창의
  `data-mado-mention` 속성 (NiceGUI 3 는 요소에 DOM id 를 달지 않습니다)
* 서버 → 브라우저: `MadoMention.show(id, seq, items)` — 늦게 도착한 옛 답(`seq`)은 버립니다.
"""

MENTION_QUERY_EVENT = "mado_mention_query"

MENTION_INPUT_CLASS = "mado-mention-input"

MENTION_JS = """
(function () {
    if (window.MadoMention) return;

    var ICONS = { file: 'description', dir: 'folder', agent: 'smart_toy', skill: 'menu_book' };
    var TOKEN = /(^|[\\s(\\[{])@("[^"\\n]*|[^\\s"@]*)$/;

    var state = { id: null, input: null, seq: 0, start: -1, items: [], index: 0, open: false, dismissedAt: -1 };
    var popup = null;
    var closeTimer = null;

    function escapeHtml(s) {
        return String(s).replace(/[&<>"']/g, function (c) {
            return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
        });
    }

    function hostOf(el) {
        return el && el.closest ? el.closest('.""" + MENTION_INPUT_CLASS + """') : null;
    }

    function nativeOf(el) {
        if (!el) return null;
        if (el.matches && el.matches('textarea, input')) return el;
        return el.querySelector('textarea, input');
    }

    // QInput 은 속성을 바깥 label 이 아니라 안쪽 입력 요소에 넘깁니다.
    function mentionIdOf(host) {
        var input = nativeOf(host);
        return (input && input.getAttribute('data-mado-mention')) || host.getAttribute('data-mado-mention');
    }

    function ensurePopup() {
        if (popup) return popup;
        popup = document.createElement('div');
        popup.className = 'mado-mention-popup';
        popup.setAttribute('role', 'listbox');
        popup.hidden = true;
        popup.addEventListener('mousedown', function (e) {
            // 입력창이 포커스를 잃어 창이 닫히기 전에 고릅니다.
            e.preventDefault();
            var row = e.target.closest('[data-index]');
            if (row) pick(Number(row.getAttribute('data-index')));
        });
        document.body.appendChild(popup);
        return popup;
    }

    function close() {
        state.open = false;
        state.items = [];
        if (popup) popup.hidden = true;
    }

    function place() {
        if (!popup || !state.input) return;
        var rect = state.input.getBoundingClientRect();
        popup.style.left = Math.max(8, rect.left) + 'px';
        popup.style.bottom = Math.max(8, window.innerHeight - rect.top + 6) + 'px';
        popup.style.width = Math.min(Math.max(rect.width, 280), 560) + 'px';
    }

    function render() {
        var box = ensurePopup();
        if (!state.items.length) {
            box.innerHTML = '<div class="mado-mention-empty">일치하는 전문가·스킬·파일이 없습니다</div>';
        } else {
            box.innerHTML = state.items.map(function (item, i) {
                return '<div class="mado-mention-item' + (i === state.index ? ' active' : '') +
                    '" role="option" data-index="' + i + '">' +
                    '<i class="material-icons mado-mention-icon mado-mention-' + escapeHtml(item.kind) + '">' +
                    (ICONS[item.kind] || 'label') + '</i>' +
                    '<span class="mado-mention-label">' + escapeHtml(item.label) + '</span>' +
                    '<span class="mado-mention-detail">' + escapeHtml(item.detail || '') + '</span></div>';
            }).join('');
            var active = box.querySelector('.active');
            if (active && active.scrollIntoView) active.scrollIntoView({ block: 'nearest' });
        }
        place();
        box.hidden = false;
        state.open = true;
    }

    function detect(host) {
        var input = nativeOf(host);
        if (!input) return;
        var caret = input.selectionStart;
        if (caret == null || caret !== input.selectionEnd) { close(); return; }
        var before = input.value.slice(0, caret);
        var m = TOKEN.exec(before);
        if (!m) { close(); state.dismissedAt = -1; return; }
        var start = before.length - m[2].length - 1;
        if (start === state.dismissedAt) return;   // Esc 로 닫은 그 언급
        state.id = mentionIdOf(host);
        state.input = input;
        state.start = start;
        state.seq += 1;
        var query = m[2].charAt(0) === '"' ? m[2].slice(1) : m[2];
        emitEvent('""" + MENTION_QUERY_EVENT + """', { id: state.id, seq: state.seq, query: query });
    }

    function pick(i) {
        var item = state.items[i];
        var input = state.input;
        if (!item || !input) return;
        var caret = input.selectionStart;
        var value = input.value;
        var insert = item.insert + ' ';
        input.value = value.slice(0, state.start) + insert + value.slice(caret);
        var pos = state.start + insert.length;
        input.setSelectionRange(pos, pos);
        // Vue(QInput) 가 값을 알게 합니다. 그래야 서버 쪽 값도 바뀝니다.
        input.dispatchEvent(new Event('input', { bubbles: true }));
        close();
        input.focus();
    }

    document.addEventListener('input', function (e) {
        var host = hostOf(e.target);
        if (host) detect(host);
    }, true);

    document.addEventListener('click', function (e) {
        var host = hostOf(e.target);
        if (host) detect(host);
    }, true);

    document.addEventListener('compositionend', function (e) {
        var host = hostOf(e.target);
        if (host) detect(host);
    }, true);

    document.addEventListener('keydown', function (e) {
        if (!state.open || !hostOf(e.target)) return;
        if (e.isComposing || e.keyCode === 229) return;
        var n = state.items.length;
        var handled = true;
        if (e.key === 'Escape') {
            state.dismissedAt = state.start;
            close();
        } else if (!n) {
            handled = false;
        } else if (e.key === 'ArrowDown') {
            state.index = (state.index + 1) % n; render();
        } else if (e.key === 'ArrowUp') {
            state.index = (state.index - 1 + n) % n; render();
        } else if ((e.key === 'Enter' && !e.shiftKey && !e.ctrlKey && !e.altKey && !e.metaKey) || e.key === 'Tab') {
            pick(state.index);
        } else {
            handled = false;
        }
        if (handled) {
            e.preventDefault();
            e.stopPropagation();
            e.stopImmediatePropagation();
        }
    }, true);

    document.addEventListener('focusout', function (e) {
        if (!hostOf(e.target)) return;
        clearTimeout(closeTimer);
        closeTimer = setTimeout(function () {
            if (!hostOf(document.activeElement)) close();
        }, 150);
    }, true);

    window.addEventListener('resize', place);

    window.MadoMention = {
        show: function (id, seq, items) {
            if (id !== state.id || seq !== state.seq) return;   // 늦게 온 옛 답
            if (!state.input || document.activeElement !== state.input) return;
            state.items = items || [];
            state.index = 0;
            render();
        },
        close: close,
        // 업로드 직후 입력창에 언급을 넣습니다. 커서가 없으면 끝에.
        insert: function (id, text) {
            var input = nativeOf(document.querySelector('[data-mado-mention="' + id + '"]'));
            if (!input) return;
            var value = input.value;
            var caret = input.selectionStart == null ? value.length : input.selectionStart;
            var pad = caret > 0 && !/\\s$/.test(value.slice(0, caret)) ? ' ' : '';
            var insert = pad + text + ' ';
            input.value = value.slice(0, caret) + insert + value.slice(caret);
            var pos = caret + insert.length;
            input.dispatchEvent(new Event('input', { bubbles: true }));
            input.focus();
            input.setSelectionRange(pos, pos);
        },
        _state: state
    };
})();
"""
