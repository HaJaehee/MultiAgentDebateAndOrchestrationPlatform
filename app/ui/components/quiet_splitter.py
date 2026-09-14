"""드래그가 가벼운 스플리터."""

from nicegui import ui


class QuietSplitter(ui.splitter):
    """`ui.splitter` 에 두 가지를 더합니다.

    1. **서버가 값을 되돌려 보내지 않습니다** (`LOOPBACK = False`).
       Quasar 스플리터는 끄는 동안 창 폭을 브라우저에서 직접 바꾸고, **놓을 때 한 번**
       값을 서버로 보냅니다. 되돌림이 켜져 있으면 서버가 그 값을 곧바로 다시 보내, 막
       놓은 순간 양쪽 창을 한 번 더 배치했습니다. 브라우저가 이미 그 위치에 있으니
       되돌려 받을 이유가 없습니다. 서버는 여전히 값을 받으므로 `splitter.value` 는
       최신이고, 서버에서 값을 **바꾸면** 그때는 브라우저로 나갑니다.

    2. **끄는 동안 창 내용을 고정합니다** (`mado-freeze-on-drag`, `SPLITTER_FREEZE_JS`).
       Quasar 는 마우스가 움직일 때마다 창의 `style.width` 를 바꾸고, 그때마다 브라우저는
       창 안의 발언 카드·보고서·다이어그램을 새 폭으로 다시 배치했습니다. 이제는 잡는
       순간 각 창 내용의 폭을 그대로 묶어 두었다가 **놓을 때 한 번만** 새 폭으로
       배치합니다. 대가는 끄는 동안 내용이 실시간으로 따라오지 않는 것입니다 —
       좁아지는 쪽은 잘려 보이고, 넓어지는 쪽은 옆이 비어 보입니다.
    """

    LOOPBACK = False

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.classes("mado-freeze-on-drag")


# 잡는 순간 창 내용의 폭을 묶고, 놓는 순간 풉니다.
#
# 창 자체(`.q-splitter__panel`)의 폭은 Quasar 가 계속 바꾸게 둡니다. 묶는 것은 그 **안의**
# 요소입니다. 폭이 px 로 고정되고 높이도 그대로이면, 브라우저는 창이 넓어지거나 좁아져도
# 그 안을 다시 배치하지 않고 앞서 계산한 결과를 씁니다.
#
# 이벤트는 문서의 **캡처 단계**에서 받습니다. Quasar 의 드래그 지시자는 이벤트 전파를
# 멈추므로(`stop`), 버블 단계에서 기다리면 받지 못합니다.
#
# 폭을 재는 시점은 누르는 순간입니다. 아직 아무것도 바뀌지 않았으므로 지금 모습 그대로의
# 폭입니다. 누르고 끌지 않고 떼면 같은 폭을 묶었다 푸는 것이라 배치가 바뀌지 않습니다.
#
# 놓치는 경우를 대비해 창이 포커스를 잃을 때도 풉니다 (창 밖에서 떼는 경우 등).
SPLITTER_FREEZE_JS = """
(function () {
    if (window.__madoSplitterFreeze) return;
    window.__madoSplitterFreeze = true;

    var frozen = null;

    function freeze(root) {
        if (frozen) release();
        var items = [];
        Array.prototype.forEach.call(root.children, function (panel) {
            if (!panel.classList.contains('q-splitter__panel')) return;
            Array.prototype.forEach.call(panel.children, function (child) {
                var width = child.getBoundingClientRect().width;
                items.push({
                    el: child,
                    width: child.style.getPropertyValue('width'),
                    widthPriority: child.style.getPropertyPriority('width'),
                });
                child.style.setProperty('width', width + 'px', 'important');
            });
        });
        root.classList.add('mado-splitter-frozen');
        frozen = { root: root, items: items };
    }

    function release() {
        if (!frozen) return;
        frozen.items.forEach(function (item) {
            if (item.width) item.el.style.setProperty('width', item.width, item.widthPriority);
            else item.el.style.removeProperty('width');
        });
        frozen.root.classList.remove('mado-splitter-frozen');
        frozen = null;
    }

    function onDown(event) {
        if (event.type === 'mousedown' && event.button !== 0) return;
        var area = event.target && event.target.closest && event.target.closest('.q-splitter__separator-area');
        if (!area) return;
        var root = area.closest('.q-splitter');
        if (!root || !root.classList.contains('mado-freeze-on-drag')) return;
        freeze(root);
    }

    document.addEventListener('mousedown', onDown, true);
    document.addEventListener('touchstart', onDown, { capture: true, passive: true });
    ['mouseup', 'touchend', 'touchcancel'].forEach(function (type) {
        document.addEventListener(type, release, true);
    });
    window.addEventListener('blur', release);

    window.MadoSplitterFreeze = { freeze: freeze, release: release, isFrozen: function () { return !!frozen; } };
})();
"""
