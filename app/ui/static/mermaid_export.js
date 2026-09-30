/**
 * Mermaid Diagram Export & Clipboard Utility
 * MADO: Multi-Agent Debate & Orchestration Platform
 */

(function() {
    window.MadoMermaid = {
        /**
         * SVG 엘리먼트를 Canvas 렌더링에 안전한 순수 SVG 로 정제합니다.
         * foreignObject 및 외부 @import 등을 변환/제거하여 Canvas Tainting 을 원천 차단합니다.
         */
        sanitizeSvgForCanvas: function(svgElement) {
            const clone = svgElement.cloneNode(true);
            clone.setAttribute('xmlns', 'http://www.w3.org/2000/svg');
            clone.setAttribute('xmlns:xlink', 'http://www.w3.org/1999/xlink');

            // 1. 외부 CSS 및 @import, @font-face 제거 (Tainting 방지)
            clone.querySelectorAll('style').forEach(function(styleTag) {
                let css = styleTag.textContent || '';
                css = css.replace(/@import\s+url\([^)]+\);?/gi, '');
                css = css.replace(/@font-face\s*\{[^}]+\}/gi, '');
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

                const lines = textContent.split('\n').map(function(l) { return l.trim(); }).filter(Boolean);
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

            // Canvas 용 정제된 복제본 생성
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

            // 원본 SVG 문자열 (SVG 파일 다운로드 및 복사용)
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

                        // 배경을 깔끔한 흰색으로 채웁니다.
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
                                    window.Quasar.Notify.create({ type: 'positive', message: '다이어그램 이미지를 클립보드에 복사했습니다. (Ctrl+V로 붙여넣기)', position: 'top' });
                                }
                                resolve(true);
                                return;
                            } catch (clipErr) {
                                console.warn('[MadoMermaid] ClipboardItem write failed:', clipErr);
                            }
                        }
                        if (window.Quasar) {
                            window.Quasar.Notify.create({ type: 'info', message: '이 환경(HTTP 또는 보안 제한)에서는 클립보드 이미지 직접 쓰기가 지원되지 않습니다. PNG 다운로드를 사용해 주십시오.', position: 'top' });
                        }
                        resolve(false);
                    }, 'image/png');
                });
            } catch (err) {
                console.error('[MadoMermaid] Copy image error:', err);
                if (window.Quasar) {
                    window.Quasar.Notify.create({ type: 'negative', message: '이미지 복사에 실패했습니다: ' + err.message, position: 'top' });
                }
                return false;
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
                        window.Quasar.Notify.create({ type: 'positive', message: 'SVG 코드를 클립보드에 복사했습니다.', position: 'top' });
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
                    window.Quasar.Notify.create({ type: 'positive', message: 'SVG 코드를 클립보드에 복사했습니다.', position: 'top' });
                }
                return true;
            } finally {
                ta.remove();
            }
        }
    };
})();
