"""Mermaid 클라이언트 추출 및 내보내기 JS 스크립트 정의 모듈."""

MERMAID_EXPORT_JS = """
(function() {
    window.MadoMermaid = {
        sanitizeSvgForCanvas: function(svgElement) {
            const clone = svgElement.cloneNode(true);
            clone.setAttribute('xmlns', 'http://www.w3.org/2000/svg');
            clone.setAttribute('xmlns:xlink', 'http://www.w3.org/1999/xlink');

            // 1. 외부 CSS 및 @import, @font-face 제거 (Tainting 방지)
            clone.querySelectorAll('style').forEach(function(styleTag) {
                let css = styleTag.textContent || '';
                css = css.replace(/@import\\s+url\\([^)]+\\);?/gi, '');
                css = css.replace(/@font-face\\s*\\{[^}]+\\}/gi, '');
                styleTag.textContent = css;
            });

            // 2. <foreignObject> 를 순수 SVG <text> 및 <tspan> 으로 변환 (핵심 Tainting 해결)
            const foreignObjects = clone.querySelectorAll('foreignObject');
            foreignObjects.forEach(function(fo) {
                const x = parseFloat(fo.getAttribute('x') || 0);
                const y = parseFloat(fo.getAttribute('y') || 0);
                const width = parseFloat(fo.getAttribute('width') || 0);
                const height = parseFloat(fo.getAttribute('height') || 0);

                const textContent = (fo.textContent || '').trim();
                if (!textContent) {
                    fo.remove();
                    return;
                }

                const textElem = document.createElementNS('http://www.w3.org/2000/svg', 'text');
                textElem.setAttribute('x', (x + width / 2).toString());
                textElem.setAttribute('y', (y + height / 2).toString());
                textElem.setAttribute('text-anchor', 'middle');
                textElem.setAttribute('dominant-baseline', 'central');
                textElem.setAttribute('alignment-baseline', 'central');
                textElem.setAttribute('fill', '#0f172a');
                textElem.setAttribute('font-family', '-apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif');
                textElem.setAttribute('font-size', '13px');
                textElem.setAttribute('font-weight', '500');

                const lines = textContent.split('\\n').map(function(l) { return l.trim(); }).filter(Boolean);
                if (lines.length > 1) {
                    const lineHeight = 16;
                    const startY = y + height / 2 - ((lines.length - 1) * lineHeight) / 2;
                    textElem.textContent = '';
                    lines.forEach(function(line, idx) {
                        const tspan = document.createElementNS('http://www.w3.org/2000/svg', 'tspan');
                        tspan.setAttribute('x', (x + width / 2).toString());
                        tspan.setAttribute('y', (startY + idx * lineHeight).toString());
                        tspan.textContent = line;
                        textElem.appendChild(tspan);
                    });
                } else {
                    textElem.textContent = textContent;
                }

                if (fo.parentNode) {
                    fo.parentNode.replaceChild(textElem, fo);
                }
            });

            // 3. 외부 image 링크 제거
            clone.querySelectorAll('image').forEach(function(img) {
                const href = img.getAttribute('href') || img.getAttribute('xlink:href') || '';
                if (href.startsWith('http://') || href.startsWith('https://')) {
                    img.remove();
                }
            });

            return clone;
        },

        getSvgData: function(wrapperId) {
            const wrapper = document.getElementById(wrapperId) || document.querySelector('#' + wrapperId);
            if (!wrapper) {
                console.warn('[MadoMermaid] Wrapper not found:', wrapperId);
                return null;
            }
            const svg = wrapper.querySelector('svg');
            if (!svg) {
                console.warn('[MadoMermaid] SVG not found in wrapper:', wrapperId);
                return null;
            }

            const sanitized = this.sanitizeSvgForCanvas(svg);

            let width = 0;
            let height = 0;

            if (svg.viewBox && svg.viewBox.baseVal && svg.viewBox.baseVal.width > 0) {
                width = svg.viewBox.baseVal.width;
                height = svg.viewBox.baseVal.height;
            } else {
                try {
                    const bbox = svg.getBBox();
                    if (bbox && bbox.width > 0) {
                        width = bbox.width + Math.max(0, bbox.x) * 2;
                        height = bbox.height + Math.max(0, bbox.y) * 2;
                    }
                } catch (e) {}
                if (!width || !height) {
                    width = svg.clientWidth || parseFloat(svg.getAttribute('width')) || 800;
                    height = svg.clientHeight || parseFloat(svg.getAttribute('height')) || 600;
                }
            }

            width = Math.max(100, Math.ceil(width));
            height = Math.max(100, Math.ceil(height));

            sanitized.setAttribute('width', width.toString());
            sanitized.setAttribute('height', height.toString());
            if (!sanitized.getAttribute('viewBox')) {
                sanitized.setAttribute('viewBox', `0 0 ${width} ${height}`);
            }

            const serializer = new XMLSerializer();
            let svgString = serializer.serializeToString(sanitized);
            if (!svgString.includes('xmlns="http://www.w3.org/2000/svg"')) {
                svgString = svgString.replace('<svg', '<svg xmlns="http://www.w3.org/2000/svg"');
            }

            const rawClone = svg.cloneNode(true);
            rawClone.setAttribute('xmlns', 'http://www.w3.org/2000/svg');
            rawClone.setAttribute('xmlns:xlink', 'http://www.w3.org/1999/xlink');
            rawClone.setAttribute('width', width.toString());
            rawClone.setAttribute('height', height.toString());
            if (!rawClone.getAttribute('viewBox')) {
                rawClone.setAttribute('viewBox', `0 0 ${width} ${height}`);
            }
            let rawSvgString = serializer.serializeToString(rawClone);
            if (!rawSvgString.includes('xmlns="http://www.w3.org/2000/svg"')) {
                rawSvgString = rawSvgString.replace('<svg', '<svg xmlns="http://www.w3.org/2000/svg"');
            }

            return {
                svgString: svgString,
                rawSvgString: rawSvgString,
                width: width,
                height: height
            };
        },

        renderToCanvas: function(svgData, scale) {
            scale = scale || 2;
            return new Promise((resolve, reject) => {
                const base64Svg = window.btoa(unescape(encodeURIComponent(svgData.svgString)));
                const dataUrl = 'data:image/svg+xml;base64,' + base64Svg;
                const img = new Image();

                img.onload = function() {
                    try {
                        const canvas = document.createElement('canvas');
                        canvas.width = svgData.width * scale;
                        canvas.height = svgData.height * scale;
                        const ctx = canvas.getContext('2d');

                        ctx.fillStyle = '#ffffff';
                        ctx.fillRect(0, 0, canvas.width, canvas.height);

                        ctx.drawImage(img, 0, 0, canvas.width, canvas.height);
                        resolve(canvas);
                    } catch (err) {
                        reject(err);
                    }
                };

                img.onerror = function(err) {
                    reject(new Error('Failed to load SVG into image: ' + err));
                };

                img.src = dataUrl;
            });
        },

        downloadPng: async function(wrapperId, filename) {
            const svgData = this.getSvgData(wrapperId);
            if (!svgData) {
                if (window.Quasar) {
                    window.Quasar.Notify.create({ type: 'warning', message: '다이어그램 렌더링을 찾을 수 없습니다.', position: 'top' });
                }
                return false;
            }

            try {
                const canvas = await this.renderToCanvas(svgData, 2);
                canvas.toBlob((blob) => {
                    if (!blob) return;
                    const url = URL.createObjectURL(blob);
                    const a = document.createElement('a');
                    a.href = url;
                    a.download = (filename || 'diagram') + '.png';
                    document.body.appendChild(a);
                    a.click();
                    document.body.removeChild(a);
                    setTimeout(() => URL.revokeObjectURL(url), 1000);
                    if (window.Quasar) {
                        window.Quasar.Notify.create({ type: 'positive', message: `'${filename}.png' 다운로드가 시작되었습니다.`, position: 'top' });
                    }
                }, 'image/png');
                return true;
            } catch (err) {
                console.error('[MadoMermaid] PNG export error:', err);
                if (window.Quasar) {
                    window.Quasar.Notify.create({ type: 'negative', message: 'PNG 생성 중 오류가 발생했습니다: ' + err.message, position: 'top' });
                }
                return false;
            }
        },

        downloadSvg: function(wrapperId, filename) {
            const svgData = this.getSvgData(wrapperId);
            if (!svgData) {
                if (window.Quasar) {
                    window.Quasar.Notify.create({ type: 'warning', message: '다이어그램 렌더링을 찾을 수 없습니다.', position: 'top' });
                }
                return false;
            }

            const blob = new Blob([svgData.rawSvgString || svgData.svgString], { type: 'image/svg+xml;charset=utf-8' });
            const url = URL.createObjectURL(blob);
            const a = document.createElement('a');
            a.href = url;
            a.download = (filename || 'diagram') + '.svg';
            document.body.appendChild(a);
            a.click();
            document.body.removeChild(a);
            setTimeout(() => URL.revokeObjectURL(url), 1000);
            if (window.Quasar) {
                window.Quasar.Notify.create({ type: 'positive', message: `'${filename}.svg' 다운로드가 시작되었습니다.`, position: 'top' });
            }
            return true;
        },

        copyImageToClipboard: async function(wrapperId) {
            const svgData = this.getSvgData(wrapperId);
            if (!svgData) {
                if (window.Quasar) {
                    window.Quasar.Notify.create({ type: 'warning', message: '다이어그램 렌더링을 찾을 수 없습니다.', position: 'top' });
                }
                return false;
            }

            try {
                const canvas = await this.renderToCanvas(svgData, 2);
                return new Promise((resolve) => {
                    canvas.toBlob(async (blob) => {
                        if (!blob) {
                            resolve(false);
                            return;
                        }
                        if (navigator.clipboard && window.ClipboardItem && window.isSecureContext) {
                            try {
                                await navigator.clipboard.write([new ClipboardItem({ 'image/png': blob })]);
                                if (window.Quasar) {
                                    window.Quasar.Notify.create({ type: 'positive', message: '클립보드에 다이어그램 이미지가 복사되었습니다! (Ctrl+V 로 붙여넣기)', position: 'top' });
                                }
                                resolve(true);
                                return;
                            } catch (clipErr) {
                                console.warn('[MadoMermaid] ClipboardItem write failed:', clipErr);
                            }
                        }
                        if (window.Quasar) {
                            window.Quasar.Notify.create({ type: 'info', message: '이 환경(HTTP/보안제한)에서는 클립보드 이미지 직접 쓰기가 지원되지 않습니다. PNG 다운로드를 사용해주세요.', position: 'top' });
                        }
                        resolve(false);
                    }, 'image/png');
                });
            } catch (err) {
                console.error('[MadoMermaid] Copy image error:', err);
                if (window.Quasar) {
                    window.Quasar.Notify.create({ type: 'negative', message: '이미지 복사 실패: ' + err.message, position: 'top' });
                }
                return false;
            }
        },

        // 내려받을 독립 실행형 HTML 안에 심을 SVG 문자열.
        //
        // 화면에는 이미 다이어그램이 그려져 있습니다. 그 결과를 파일에 함께
        // 실어 보내면 문서가 정말로 독립적이 됩니다 — 열 때 CDN 에서 렌더러를
        // 받아 올 필요가 없으니 폐쇄망에서도 그림이 그대로 나옵니다.
        // 아직 렌더링 전이거나 문법 오류로 SVG 가 없으면 null 을 돌려주고,
        // 파이썬 쪽은 예전처럼 CDN 을 쓰는 문서를 만듭니다.
        getStandaloneSvg: function(wrapperId) {
            try {
                const svgData = this.getSvgData(wrapperId);
                if (!svgData) return null;
                return svgData.rawSvgString || svgData.svgString || null;
            } catch (err) {
                console.warn('[MadoMermaid] Could not extract SVG for HTML export:', err);
                return null;
            }
        },

        copySvgToClipboard: function(wrapperId) {
            const svgData = this.getSvgData(wrapperId);
            if (!svgData) return false;
            const text = svgData.rawSvgString || svgData.svgString;

            try {
                if (navigator.clipboard && window.isSecureContext) {
                    navigator.clipboard.writeText(text);
                    if (window.Quasar) {
                        window.Quasar.Notify.create({ type: 'positive', message: 'SVG 코드가 클립보드에 복사되었습니다!', position: 'top' });
                    }
                    return true;
                }
            } catch (e) {}

            const ta = document.createElement('textarea');
            ta.value = text;
            ta.setAttribute('readonly', '');
            ta.style.position = 'fixed';
            ta.style.top = '-1000px';
            document.body.appendChild(ta);
            ta.select();
            try {
                document.execCommand('copy');
                if (window.Quasar) {
                    window.Quasar.Notify.create({ type: 'positive', message: 'SVG 코드가 클립보드에 복사되었습니다!', position: 'top' });
                }
                return true;
            } finally {
                ta.remove();
            }
        }
    };
})();
"""


# 산출물 창의 Mermaid 다이어그램을 **이미지로** 보여줍니다.
#
# Mermaid 는 SVG 를 DOM 에 그대로 넣고, 노드 글자는 SVG 안의 HTML(`foreignObject`)로
# 그립니다. 그래서 창 폭이 바뀔 때마다 다이어그램 전체를 다시 줄이고 늘리면서 노드마다
# 글자 배치를 다시 했습니다. 서랍을 여닫거나 스플리터를 놓을 때 그 비용이 다이어그램
# 크기만큼 들었습니다.
#
# 같은 SVG 를 `<img>` 로 넣으면 브라우저는 그림 한 장으로 다루어, 크기가 바뀌어도 안쪽을
# 다시 배치하지 않습니다.
#
# **원본 SVG 는 지우지 않고 `display:none` 으로 둡니다.** 숨긴 요소는 배치도 칠하기도 하지
# 않으니 비용이 없고, 복사·다운로드(`MadoMermaid.getSvgData`)는 여전히 그 SVG 를 찾아
# `viewBox` 로 크기를 잽니다. 그래서 `viewBox` 가 없는 SVG 는 이미지로 바꾸지 않습니다 —
# 숨기면 내보내기가 크기를 잴 방법이 없습니다.
#
# 원본은 **이미지가 실제로 불러와진 뒤에** 숨깁니다. 불러오기에 실패하면 이미지를 치우고
# 원본을 그대로 보여 줍니다. 대가는 다이어그램 안 글자를 끌어서 선택할 수 없다는 것입니다.
#
# Mermaid 는 그리기를 비동기로 끝내므로 DOM 변화를 지켜보다가 `.mado-mermaid` 바로 아래에
# SVG 가 들어오는 순간 바꿉니다. 다이어그램을 다시 그리면 NiceGUI 가 그 안을 통째로 갈아
# 끼우므로 이전 이미지도 함께 사라지고, 새 SVG 가 다시 이미지로 바뀝니다.
MERMAID_IMAGE_JS = """
(function () {
    if (window.__madoMermaidImage) return;
    window.__madoMermaidImage = true;

    function toImage(svg) {
        var box = svg.parentElement;
        if (!box || !box.classList.contains('mado-mermaid')) return;
        if (svg.dataset.madoImaged === '1') return;
        var vb = svg.viewBox && svg.viewBox.baseVal;
        if (!vb || !(vb.width > 0) || !(vb.height > 0)) return;
        svg.dataset.madoImaged = '1';

        var clone = svg.cloneNode(true);
        clone.setAttribute('xmlns', 'http://www.w3.org/2000/svg');
        clone.setAttribute('width', String(vb.width));
        clone.setAttribute('height', String(vb.height));
        var xml = new XMLSerializer().serializeToString(clone);

        var img = new Image();
        img.className = 'mado-mermaid-image';
        img.alt = 'Mermaid diagram';
        img.decoding = 'async';
        img.onload = function () {
            if (img.isConnected) svg.classList.add('mado-mermaid-source');
        };
        img.onerror = function () {
            img.remove();
            svg.classList.remove('mado-mermaid-source');
        };
        img.src = 'data:image/svg+xml;charset=utf-8,' + encodeURIComponent(xml);
        svg.insertAdjacentElement('afterend', img);
    }

    function scan(node) {
        if (node.nodeType !== 1) return;
        if (node.tagName.toLowerCase() === 'svg') { toImage(node); return; }
        if (node.querySelectorAll) node.querySelectorAll('.mado-mermaid > svg').forEach(toImage);
    }

    function start() {
        new MutationObserver(function (records) {
            records.forEach(function (record) { record.addedNodes.forEach(scan); });
        }).observe(document.body, { childList: true, subtree: true });
        document.querySelectorAll('.mado-mermaid > svg').forEach(toImage);
    }

    if (document.body) start();
    else document.addEventListener('DOMContentLoaded', start);
})();
"""
