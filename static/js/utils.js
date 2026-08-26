/**
 * utils.js — Shared utility functions.
 *
 * Loaded first (before all other app scripts) so helpers like fmtNum and esc
 * are available everywhere.
 */

// ── Time formatting (ISO UTC → browser-local display) ────────────────────

function _pad2(n) { return String(n).padStart(2, '0'); }

/** ISO UTC timestamp ("YYYY-MM-DDTHH:MM:SS[.fff]Z") → local "YYYY-MM-DD HH:MM:SS". */
function fmtLocal(isoStr) {
    if (isoStr == null || isoStr === '') return '';
    var d = new Date(String(isoStr));
    if (isNaN(d.getTime())) return '';
    return d.getFullYear() + '-' + _pad2(d.getMonth() + 1) + '-' + _pad2(d.getDate())
        + ' ' + _pad2(d.getHours()) + ':' + _pad2(d.getMinutes()) + ':' + _pad2(d.getSeconds());
}

/** ISO UTC timestamp → local "HH:MM" (for chart x-axis labels). */
function fmtLocalHHMM(isoStr) {
    var s = fmtLocal(isoStr);
    return s ? s.slice(11, 16) : '';
}

function normalizeMinute(minute) {
    return ((Math.round(minute) % 1440) + 1440) % 1440;
}

/** Convert a recurring local minute-of-day to UTC minute-of-day. */
function localMinuteToUtc(minute, offsetMinutes) {
    var offset = offsetMinutes == null ? new Date().getTimezoneOffset() : offsetMinutes;
    return normalizeMinute(minute + offset);
}

/** Convert a recurring UTC minute-of-day to browser-local minute-of-day. */
function utcMinuteToLocal(minute, offsetMinutes) {
    var offset = offsetMinutes == null ? new Date().getTimezoneOffset() : offsetMinutes;
    return normalizeMinute(minute - offset);
}

function _parseCalendarDate(value) {
    if (!value) return null;
    var p = String(value).split('-').map(Number);
    if (p.length !== 3 || p.some(isNaN)) return null;
    return p;
}

function _formatUtcDate(date) {
    return date.getUTCFullYear() + '-' + _pad2(date.getUTCMonth() + 1) + '-' + _pad2(date.getUTCDate());
}

function _formatLocalDate(date) {
    return date.getFullYear() + '-' + _pad2(date.getMonth() + 1) + '-' + _pad2(date.getDate());
}

/** Local date → the UTC date whose midnight is shown on that local date. */
function localDateToUtcDate(localDate) {
    var p = _parseCalendarDate(localDate);
    if (!p) return '';
    for (var delta = -1; delta <= 1; delta++) {
        var candidate = new Date(Date.UTC(p[0], p[1] - 1, p[2] + delta));
        if (_formatLocalDate(candidate) === String(localDate)) return _formatUtcDate(candidate);
    }
    return '';
}

/** UTC calendar date "YYYY-MM-DD" → local calendar date "YYYY-MM-DD". */
function utcDateToLocalDate(utcDate) {
    var p = _parseCalendarDate(utcDate);
    if (!p) return '';
    var d = new Date(Date.UTC(p[0], p[1] - 1, p[2]));
    if (isNaN(d.getTime())) return '';
    return _formatLocalDate(d);
}

/** Local date "YYYY-MM-DD" → [UTC start ISO, UTC end ISO] covering the local day. */
function localDateToUtcRange(localDate) {
    if (!localDate) return null;
    var p = String(localDate).split('-').map(Number);
    if (p.length !== 3 || isNaN(p[0]) || isNaN(p[1]) || isNaN(p[2])) return null;
    var start = new Date(p[0], p[1] - 1, p[2]);
    if (isNaN(start.getTime())) return null;
    var end = new Date(p[0], p[1] - 1, p[2] + 1);
    return [start.toISOString(), new Date(end.getTime() - 1).toISOString()];
}

// ── Number formatting ───────────────────────────────────────────────────────

function fmtNum(n) {
    if (n == null || isNaN(n)) return '--';
    if (n >= 100_000_000_000) return (n / 1_000_000_000).toLocaleString('en-US', {minimumFractionDigits: 2, maximumFractionDigits: 2}) + ' B';
    if (n >= 100_000_000) return (n / 1_000_000).toLocaleString('en-US', {minimumFractionDigits: 2, maximumFractionDigits: 2}) + ' M';
    if (n >= 100_000) return (n / 1_000).toLocaleString('en-US', {minimumFractionDigits: 1, maximumFractionDigits: 1}) + ' K';
    return n.toLocaleString('en-US');
}

// ── HTML escaping ───────────────────────────────────────────────────────────

function esc(s) {
    if (s == null) return '';
    return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

// ── Themeable select menus ───────────────────────────────────────────────
// Browsers do not expose enough styling hooks for the native option popup.
// Keep each native select as the form/value source, but mirror it with an
// accessible button + listbox so the expanded state belongs to our theme too.
(function installThemedSelects() {
    var instances = new WeakMap();
    var activeInstance = null;
    var nextMenuId = 0;
    var observer = null;
    var nativeValueDescriptor = typeof HTMLSelectElement !== 'undefined'
        ? Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, 'value')
        : null;

    function optionIsDisabled(option) {
        var parent = option.parentElement;
        return option.disabled || (parent && parent.tagName === 'OPTGROUP' && parent.disabled);
    }

    function optionLabel(option) {
        var label = option.textContent.trim();
        return label || option.value || '请选择';
    }

    function visibleOptionIndices(instance) {
        return Array.from(instance.select.options)
            .map(function (option, index) { return option.hidden ? -1 : index; })
            .filter(function (index) { return index >= 0; });
    }

    function enabledOptionIndices(instance) {
        return visibleOptionIndices(instance).filter(function (index) {
            return !optionIsDisabled(instance.select.options[index]);
        });
    }

    function preferredActiveIndex(instance) {
        var selected = instance.select.selectedIndex;
        var enabled = enabledOptionIndices(instance);
        if (selected >= 0 && enabled.indexOf(selected) >= 0) return selected;
        var visible = visibleOptionIndices(instance);
        return enabled[0] != null ? enabled[0] : (visible[0] != null ? visible[0] : -1);
    }

    function updateHighlight(instance, scrollIntoView) {
        var optionElements = instance.optionElements;
        Object.keys(optionElements).forEach(function (index) {
            optionElements[index].dataset.highlighted = String(Number(index) === instance.activeIndex);
        });
        var activeOption = optionElements[instance.activeIndex];
        if (activeOption) {
            instance.trigger.setAttribute('aria-activedescendant', activeOption.id);
            if (scrollIntoView) activeOption.scrollIntoView({ block: 'nearest' });
        } else {
            instance.trigger.removeAttribute('aria-activedescendant');
        }
    }

    function renderOptions(instance) {
        var select = instance.select;
        var previousActive = instance.activeIndex;
        var menu = instance.menu;
        var optionElements = Object.create(null);
        var visible = visibleOptionIndices(instance);

        menu.innerHTML = '';
        visible.forEach(function (index) {
            var option = select.options[index];
            var optionElement = document.createElement('div');
            var disabled = optionIsDisabled(option);
            optionElement.className = 'themed-select__option';
            optionElement.id = instance.menu.id + '-option-' + index;
            optionElement.setAttribute('role', 'option');
            optionElement.setAttribute('aria-selected', String(option.selected));
            optionElement.setAttribute('aria-disabled', String(disabled));
            optionElement.dataset.index = String(index);
            optionElement.textContent = optionLabel(option);
            optionElement.addEventListener('pointerdown', function (event) {
                // Keep focus on the combobox while choosing with a pointer.
                event.preventDefault();
            });
            optionElement.addEventListener('pointerenter', function () {
                if (!disabled) {
                    instance.activeIndex = index;
                    updateHighlight(instance, false);
                }
            });
            optionElement.addEventListener('click', function () {
                if (!disabled) chooseOption(instance, index);
            });
            menu.appendChild(optionElement);
            optionElements[index] = optionElement;
        });

        if (!visible.length) {
            var empty = document.createElement('div');
            empty.className = 'themed-select__empty';
            empty.textContent = '暂无选项';
            menu.appendChild(empty);
        }

        instance.optionElements = optionElements;
        if (activeInstance === instance && instance.open &&
            optionElements[previousActive] && !optionIsDisabled(select.options[previousActive])) {
            instance.activeIndex = previousActive;
        } else {
            instance.activeIndex = preferredActiveIndex(instance);
        }
        updateHighlight(instance, false);
    }

    function syncInstance(instance) {
        var select = instance.select;
        var selected = select.selectedIndex >= 0 ? select.options[select.selectedIndex] : null;
        var label = selected ? optionLabel(selected) : '请选择';

        instance.triggerLabel.textContent = label;
        instance.trigger.classList.toggle('themed-select__trigger--placeholder', !selected);
        instance.trigger.disabled = select.disabled;
        instance.trigger.setAttribute('aria-disabled', String(select.disabled));
        if (select.required) instance.trigger.setAttribute('aria-required', 'true');
        else instance.trigger.removeAttribute('aria-required');

        ['aria-label', 'aria-labelledby', 'aria-describedby'].forEach(function (attribute) {
            var value = select.getAttribute(attribute);
            if (value) instance.trigger.setAttribute(attribute, value);
            else instance.trigger.removeAttribute(attribute);
        });
        if (select.title) instance.trigger.title = select.title;
        else instance.trigger.removeAttribute('title');

        instance.wrapper.classList.toggle('themed-select--disabled', select.disabled);
        instance.wrapper.classList.toggle(
            'themed-select--invalid', select.getAttribute('aria-invalid') === 'true'
        );
        renderOptions(instance);
        if (activeInstance === instance && instance.open) positionInstance(instance);
    }

    function patchValueSetter(instance) {
        if (!nativeValueDescriptor || !nativeValueDescriptor.get || !nativeValueDescriptor.set) return;
        try {
            Object.defineProperty(instance.select, 'value', {
                configurable: true,
                enumerable: true,
                get: function () { return nativeValueDescriptor.get.call(this); },
                set: function (value) {
                    nativeValueDescriptor.set.call(this, value);
                    syncInstance(instance);
                },
            });
        } catch (_) {
            // A browser may reject an instance-level accessor; the mutation
            // observer and native change event still keep the mirror current.
        }
    }

    function positionInstance(instance) {
        if (activeInstance !== instance || !instance.open) return;
        var rect = instance.trigger.getBoundingClientRect();
        var viewportPadding = 12;
        var gap = 8;
        var width = Math.min(rect.width, window.innerWidth - viewportPadding * 2);
        var below = window.innerHeight - rect.bottom - viewportPadding;
        var above = rect.top - viewportPadding;
        var openUp = below < 220 && above > below;
        var available = Math.max(96, (openUp ? above : below) - gap);

        instance.menu.style.width = Math.max(1, width) + 'px';
        instance.menu.style.maxHeight = Math.min(320, available) + 'px';
        instance.menu.dataset.placement = openUp ? 'top' : 'bottom';

        var left = Math.min(
            Math.max(viewportPadding, rect.left),
            Math.max(viewportPadding, window.innerWidth - width - viewportPadding)
        );
        var height = instance.menu.offsetHeight;
        var top = openUp ? rect.top - height - gap : rect.bottom + gap;
        top = Math.max(viewportPadding, Math.min(top, window.innerHeight - height - viewportPadding));
        instance.menu.style.left = left + 'px';
        instance.menu.style.top = top + 'px';
    }

    function closeInstance(instance, restoreFocus) {
        if (!instance || (!instance.open && activeInstance !== instance)) return;
        instance.open = false;
        instance.trigger.setAttribute('aria-expanded', 'false');
        instance.menu.setAttribute('aria-hidden', 'true');
        instance.menu.classList.remove('themed-select__menu--open');
        instance.wrapper.classList.remove('themed-select--open');
        instance.trigger.removeAttribute('aria-activedescendant');
        if (activeInstance === instance) activeInstance = null;
        if (restoreFocus && !instance.trigger.disabled) {
            instance.trigger.focus({ preventScroll: true });
        }
    }

    function openInstance(instance) {
        if (instance.select.disabled) return;
        if (activeInstance && activeInstance !== instance) closeInstance(activeInstance, false);
        activeInstance = instance;
        instance.open = true;
        instance.activeIndex = preferredActiveIndex(instance);
        instance.trigger.setAttribute('aria-expanded', 'true');
        instance.menu.setAttribute('aria-hidden', 'false');
        instance.menu.dataset.placement = 'bottom';
        instance.menu.classList.add('themed-select__menu--open');
        instance.wrapper.classList.add('themed-select--open');
        document.body.appendChild(instance.menu);
        updateHighlight(instance, false);
        positionInstance(instance);

        // Preserve existing inline onfocus hooks (the aggregate model picker
        // loads its model list when the user first opens it).
        instance.select.dispatchEvent(new Event('focus'));
        positionInstance(instance);
    }

    function chooseOption(instance, index) {
        var option = instance.select.options[index];
        if (!option || optionIsDisabled(option)) return;
        var oldValue = instance.select.value;
        var oldIndex = instance.select.selectedIndex;
        instance.select.selectedIndex = index;
        syncInstance(instance);
        if (oldValue !== instance.select.value || oldIndex !== instance.select.selectedIndex) {
            instance.select.dispatchEvent(new Event('input', { bubbles: true }));
            instance.select.dispatchEvent(new Event('change', { bubbles: true }));
        }
        closeInstance(instance, true);
    }

    function moveActive(instance, delta) {
        var enabled = enabledOptionIndices(instance);
        if (!enabled.length) return;
        var current = enabled.indexOf(instance.activeIndex);
        if (current < 0) current = enabled.indexOf(preferredActiveIndex(instance));
        var next = Math.max(0, Math.min(enabled.length - 1, current + delta));
        instance.activeIndex = enabled[next];
        updateHighlight(instance, true);
    }

    function handleTriggerKeydown(instance, event) {
        var key = event.key;
        if (key === 'Escape') {
            if (instance.open) {
                event.preventDefault();
                closeInstance(instance, true);
            }
            return;
        }
        if (key === 'Tab') {
            closeInstance(instance, false);
            return;
        }
        if (key === 'Enter' || key === ' ') {
            event.preventDefault();
            if (instance.open) chooseOption(instance, instance.activeIndex);
            else openInstance(instance);
            return;
        }
        if (key === 'ArrowDown' || key === 'ArrowUp') {
            event.preventDefault();
            var wasOpen = instance.open;
            if (!wasOpen) openInstance(instance);
            else moveActive(instance, key === 'ArrowDown' ? 1 : -1);
            return;
        }
        if (key === 'Home' || key === 'End') {
            event.preventDefault();
            if (!instance.open) openInstance(instance);
            var enabled = enabledOptionIndices(instance);
            if (enabled.length) {
                instance.activeIndex = key === 'Home' ? enabled[0] : enabled[enabled.length - 1];
                updateHighlight(instance, true);
            }
        }
    }

    function enhanceSelect(select) {
        if (!select || select.multiple || select.dataset.themedSelect === 'true') return;
        var wrapper = document.createElement('div');
        var trigger = document.createElement('button');
        var triggerLabel = document.createElement('span');
        var menu = document.createElement('div');
        var menuId = 'themed-select-menu-' + (++nextMenuId);

        wrapper.className = 'themed-select';
        if (select.classList.contains('select-styled')) wrapper.classList.add('select-styled');
        if (select.id === 'keyNameSelector') wrapper.classList.add('themed-select--key-name');
        if (select.style.width) wrapper.style.width = select.style.width;

        trigger.type = 'button';
        trigger.className = 'themed-select__trigger';
        trigger.setAttribute('role', 'combobox');
        trigger.setAttribute('aria-haspopup', 'listbox');
        trigger.setAttribute('aria-expanded', 'false');
        trigger.setAttribute('aria-controls', menuId);
        triggerLabel.className = 'themed-select__label';
        trigger.appendChild(triggerLabel);

        menu.id = menuId;
        menu.className = 'themed-select__menu';
        menu.setAttribute('role', 'listbox');
        menu.setAttribute('aria-hidden', 'true');

        select.parentNode.insertBefore(wrapper, select);
        wrapper.appendChild(trigger);
        wrapper.appendChild(select);
        select.classList.add('themed-select__native');
        select.dataset.themedSelect = 'true';
        select.setAttribute('aria-hidden', 'true');
        select.tabIndex = -1;

        var instance = {
            select: select,
            wrapper: wrapper,
            trigger: trigger,
            triggerLabel: triggerLabel,
            menu: menu,
            optionElements: Object.create(null),
            activeIndex: -1,
            open: false,
        };
        instances.set(select, instance);

        trigger.addEventListener('click', function () {
            if (instance.open) closeInstance(instance, true);
            else openInstance(instance);
        });
        trigger.addEventListener('keydown', function (event) {
            handleTriggerKeydown(instance, event);
        });
        select.addEventListener('input', function () { syncInstance(instance); });
        select.addEventListener('change', function () {
            wrapper.classList.remove('themed-select--invalid');
            syncInstance(instance);
        });
        select.addEventListener('invalid', function () {
            wrapper.classList.add('themed-select--invalid');
        });
        if (typeof MutationObserver === 'function') {
            instance.observer = new MutationObserver(function () { syncInstance(instance); });
            instance.observer.observe(select, {
                subtree: true,
                childList: true,
                attributes: true,
                attributeFilter: ['disabled', 'hidden', 'label', 'required', 'selected', 'title', 'value', 'aria-label', 'aria-labelledby', 'aria-describedby'],
            });
        }
        patchValueSetter(instance);
        syncInstance(instance);
    }

    function discoverSelects(node) {
        if (!node || node.nodeType !== 1) return;
        if (node.matches && node.matches('select:not([multiple])')) enhanceSelect(node);
        if (node.querySelectorAll) {
            node.querySelectorAll('select:not([multiple])').forEach(enhanceSelect);
        }
    }

    function initThemedSelects() {
        document.querySelectorAll('select:not([multiple])').forEach(enhanceSelect);
        if (observer || typeof MutationObserver !== 'function' || !document.body) return;
        observer = new MutationObserver(function (records) {
            records.forEach(function (record) {
                record.addedNodes.forEach(discoverSelects);
            });
        });
        observer.observe(document.body, { childList: true, subtree: true });
    }

    document.addEventListener('pointerdown', function (event) {
        if (!activeInstance) return;
        if (activeInstance.wrapper.contains(event.target) || activeInstance.menu.contains(event.target)) return;
        closeInstance(activeInstance, false);
    });
    document.addEventListener('reset', function () {
        setTimeout(function () {
            document.querySelectorAll('select[data-themed-select="true"]').forEach(function (select) {
                var instance = instances.get(select);
                if (instance) syncInstance(instance);
            });
        }, 0);
    }, true);
    window.addEventListener('resize', function () {
        if (activeInstance) positionInstance(activeInstance);
    });
    window.addEventListener('scroll', function () {
        if (activeInstance) positionInstance(activeInstance);
    }, true);
    window.addEventListener('hashchange', function () {
        if (activeInstance) closeInstance(activeInstance, false);
    });

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', initThemedSelects, { once: true });
    } else {
        initThemedSelects();
    }
})();
