/* =========================================================================
 *  DOM "stripper" — keeps empty controls **and** guarantees unique
 *  data-semantic-id / data-semantic-id values by appending numeric suffixes
 * ========================================================================= */

const parse = () => {
    /* ---------- globals --------------------------------------------------- */
    const BLACKLISTED_TAGS = new Set([
        'script', 'style', 'link', 'meta', 'noscript', 'template',
        'iframe', 'svg', 'canvas', 'picture', 'video', 'audio',
        'object', 'embed'
    ]);

    const ALLOWED_ATTR = new Set([
        'id', 'href', 'src', 'type', 'name', 'value', 'placeholder',
        'checked', 'disabled', 'readonly', 'required', 'maxlength',
        'min', 'max', 'step', 'role', 'tabindex', 'alt', 'title',
        'for', 'action', 'method', 'contenteditable', 'selected',
        'multiple', 'autocomplete'
    ]);

    const PRESERVE_EMPTY_TAGS = new Set([
        'input', 'select', 'textarea', 'button', 'img', 'head', 'title'
    ]);

    /*  All semantic IDs seen so far */
    const USED_SEMANTIC_IDS = new Set();

    // Semantic IDs describe one observation, not permanent application DOM
    // state. Clear markers injected by the previous observation first;
    // otherwise a now-hidden control can collide with a newly visible one and
    // make Playwright's strict locator resolve to multiple elements.
    for (const element of document.querySelectorAll(
        '[data-semantic-id], [data-clickable]'
    )) {
        element.removeAttribute('data-semantic-id');
        element.removeAttribute('data-clickable');
    }

    /* ---------- tiny helpers -------------------------------------------- */
    const copyAllowed = (src, dst) => {
        for (const a of src.attributes) {
            if (ALLOWED_ATTR.has(a.name) ||
                a.name.startsWith('aria-') ||
                a.name.startsWith('data-')) {
                dst.setAttribute(a.name, a.value);
            }
        }
    };

    const slug = (t) =>
        t.toLowerCase().replace(/\s+/g, ' ').trim()
            .replace(/[^\w]+/g, '_').replace(/^_+|_+$/g, '').slice(0, 20); // limit to 20 chars

    const uniqueName = (base) => {
        let name = base || 'item';
        if (!USED_SEMANTIC_IDS.has(name)) {
            USED_SEMANTIC_IDS.add(name);
            return name;
        }
        let i = 1;
        while (USED_SEMANTIC_IDS.has(name + i)) i++;
        USED_SEMANTIC_IDS.add(name + i);
        return name + i;
    };

    const isEmpty = (el) => {
        if (el.nodeType === 1 &&
            PRESERVE_EMPTY_TAGS.has(el.tagName.toLowerCase())) return false;
        for (const n of el.childNodes) {
            if (n.nodeType === 3 && n.textContent.trim()) return false;
            if (n.nodeType === 1 && !isEmpty(n)) return false;
        }
        return true;
    };

    /* ---------- flatten helpers ----------------------------------------- */
    const replaceElement = (el, newTag, child) => {
        const r = document.createElement(newTag);
        for (const a of el.attributes) r.setAttribute(a.name, a.value);
        copyAllowed(child, r);
        r.innerHTML = child.innerHTML;
        return r;
    };

    const pullUpChild = (parent, child) => {
        copyAllowed(child, parent);
        parent.innerHTML = child.innerHTML;
    };

    const flatten = (el) => {
        while (el.children.length === 1) {
            const child = el.children[0];
            const p = el.tagName.toLowerCase();
            const c = child.tagName.toLowerCase();
            if (p !== 'div' && c !== 'div') break;
            el = (p === 'div' && c !== 'div')
                ? replaceElement(el, child.tagName, child)
                : (pullUpChild(el, child), el);
        }
        return el;
    };

    /* ==================================================================== */
    function automaticStripElement(original, parentName = '', parentIsClickable = false) {

        /* ---------- guard clauses ----------------------------------------- */
        if (!original || original.nodeType !== 1) return null;
        const tag = original.tagName.toLowerCase();
        if (BLACKLISTED_TAGS.has(tag)) return null;

        const style = window.getComputedStyle(original);
        const hidden = style.display === 'none' || style.visibility === 'hidden' ||
                      parseFloat(style.opacity) === 0;
        const zeroSize = original.offsetWidth === 0 && original.offsetHeight === 0;
        // Bootstrap/Vue mounts dialogs and menus below zero-sized portal
        // wrappers. Dropping such a wrapper also drops its visible controls
        // (for example GitLab's "Set a status" dialog). Preserve the wrapper
        // when it owns a genuinely rendered overlay descendant.
        const hasVisibleOverlayDescendant = zeroSize && Array.from(
            original.querySelectorAll(
                '[role="dialog"], [role="menu"], [role="listbox"], [aria-modal="true"]'
            )
        ).some((descendant) => {
            const descendantStyle = window.getComputedStyle(descendant);
            return descendantStyle.display !== 'none' &&
                   descendantStyle.visibility !== 'hidden' &&
                   parseFloat(descendantStyle.opacity) !== 0 &&
                   (descendant.offsetWidth > 0 || descendant.offsetHeight > 0);
        });
        if (hidden || (zeroSize && !hasVisibleOverlayDescendant)) return null;

        /* ---------- clone -------------------------------------------------- */
        let clone = document.createElement(original.tagName);
        copyAllowed(original, clone);

        // Capture computed styles that affect interactivity
        const computedStyle = window.getComputedStyle(original);
        if (computedStyle.pointerEvents !== 'auto') {
            clone.setAttribute('data-pointer-events', computedStyle.pointerEvents);
        }

        // Capture element state (only when focused)
        if (document.activeElement === original) {
            clone.setAttribute('data-is-focused', 'true');
        }

        /* ---------- clickability detection -------------------------------- */
        // Check if element is disabled or has pointer-events: none FIRST
        const isDisabled = original.disabled ||
                          original.hasAttribute('disabled') ||
                          style.pointerEvents === 'none';

        const probablyClickable = (() => {
            if (['button', 'select', 'summary', 'area', 'input'].includes(tag)) return true;
            if (tag === 'a' && original.hasAttribute('href')) return true;
            if (original.hasAttribute('onclick')) return true;
            const r = original.getAttribute('role');
            if (r === 'button' || r === 'link') return true;
            return style.cursor === 'pointer';
        })();

        const isClickable = !parentIsClickable && probablyClickable && !isDisabled;

        /* ---------- assign unique semantic IDs ---------------------------- */
        let thisName = '';
        if (isClickable) {
            const base = slug((original.innerText || '').trim() ||
                            original.getAttribute('title') ||
                            original.getAttribute('placeholder') ||
                            tag);
            thisName = uniqueName(parentName ? `${parentName}.${base}` : base);
            for (const e of [clone, original]) {
                e.setAttribute('data-semantic-id', thisName);
                e.setAttribute('data-clickable', 'true');
                original.setAttribute('data-semantic-id', thisName);
            }
        }

        // check for hoverable elements
        if (original.closest('[data-maybe-hoverable="true"]')) {
            clone.setAttribute('data-maybe-hoverable', 'true');
            original.setAttribute('data-maybe-hoverable', 'true');
        }

        /* ---------- INPUT / SELECT / FORM specifics ------------------------------ */
        if (tag === 'input' || tag === 'textarea' || original.hasAttribute('contenteditable')) {
            const t = original.getAttribute('type') || 'text';
            const inputIsDisabled = original.disabled || original.readOnly;

            // Only assign semantic ID if not disabled AND not already set by clickable processing
            if (!inputIsDisabled && !thisName) {
                const base = slug((original.getAttribute('placeholder') ||
                               original.getAttribute('name') ||
                               original.value || '').trim() ||
                               tag);
                thisName = uniqueName(parentName ? `${parentName}.${base}` : base);
            }

            // Only add semantic ID and input attributes if not disabled
            if (!inputIsDisabled && thisName) {
                clone.setAttribute('data-semantic-id', thisName);
                clone.setAttribute('data-value', original.value || '');
                clone.setAttribute('data-input-disabled', 'false');
                clone.setAttribute('data-can-edit', !original.readOnly ? 'true' : 'false');
                original.setAttribute('data-semantic-id', thisName);
            }

            // Attributes only reflect the initial HTML. Expose the live
            // checkbox/radio state after user interaction so the agent does
            // not toggle a selected option back off.
            if (t === 'checkbox' || t === 'radio') {
                clone.setAttribute(
                    'data-checked',
                    original.checked ? 'true' : 'false'
                );
            }


            if (!inputIsDisabled && thisName && t === 'number') {
                clone.setAttribute('data-numeric-value', original.valueAsNumber || '');
            }

            // Selection state
            if (!inputIsDisabled && thisName && original.selectionStart !== undefined) {
                clone.setAttribute('data-selection-start', original.selectionStart);
                clone.setAttribute('data-selection-end', original.selectionEnd);
            }
        }

        if (tag === 'select') {
            const selectIsDisabled = original.disabled || original.hasAttribute('disabled');

            // Only assign semantic ID if not disabled
            if (!selectIsDisabled) {
                if (!thisName) {
                    const base = slug((original.getAttribute('name') || tag));
                    thisName = uniqueName(parentName ? `${parentName}.${base}` : base);
                }

                clone.setAttribute('data-semantic-id', thisName);
                clone.setAttribute('data-value', original.value);
                clone.setAttribute('data-selected-index', original.selectedIndex);
                clone.setAttribute('data-has-multiple', original.multiple ? 'true' : 'false');

                const selectedOptions = Array.from(original.selectedOptions)
                    .map(opt => opt.value)
                    .join(',');
                clone.setAttribute('data-selected-values', selectedOptions);

                original.setAttribute('data-semantic-id', thisName);

                for (const opt of original.querySelectorAll('option')) {
                    const o = document.createElement('option');
                    o.textContent = opt.textContent.trim();
                    o.setAttribute('value', opt.value);
                    o.setAttribute('data-selected', opt.selected ? 'true' : 'false');
                    const optName = uniqueName(`${thisName}.${slug(opt.textContent)}`);
                    o.setAttribute('data-semantic-id', optName);
                    opt.setAttribute('data-semantic-id', optName);
                    clone.appendChild(o);
                }
            }
        }


        /* ---------- recurse ---------------------------------------------- */
        for (const child of original.children) {
            const cleaned = automaticStripElement(
                child,
                thisName || parentName,
                parentIsClickable || isClickable
            );
            if (cleaned &&
                (!isEmpty(cleaned) || PRESERVE_EMPTY_TAGS.has(cleaned.tagName.toLowerCase()))) {
                clone.appendChild(cleaned);
            }
        }

        /* ---------- inline text nodes ------------------------------------- */
        for (const n of original.childNodes) {
            if (n.nodeType === 3 && n.textContent.trim()) {
                clone.appendChild(document.createTextNode(n.textContent.trim()));
            }
        }

        /* ---------- flatten + prune --------------------------------------- */
        clone = flatten(clone);
        for (let i = clone.children.length - 1; i >= 0; i--) {
            const c = clone.children[i];
            if (!PRESERVE_EMPTY_TAGS.has(c.tagName.toLowerCase()) && isEmpty(c)) {
                clone.removeChild(c);
            }
        }

        return clone;
    }

    const result = automaticStripElement(document.documentElement);
    return {
        html: result.outerHTML,
        clickable_elements: Array.from(result.querySelectorAll('[data-clickable="true"]'))
            .map(el => el.getAttribute('data-semantic-id')),
        hoverable_elements: Array.from(result.querySelectorAll('[data-maybe-hoverable="true"]'))
            .map(el => el.getAttribute('data-semantic-id')),
        input_elements: Array.from(result.querySelectorAll('input[data-semantic-id], textarea[data-semantic-id], [contenteditable][data-semantic-id]'))
            .map(el => ({
                id: el.getAttribute('data-semantic-id'),
                disabled: el.hasAttribute('data-input-disabled'),
                type: el.getAttribute('type') || (el.tagName.toLowerCase() === 'textarea' ? 'textarea' : 'contenteditable'),
                value: el.value || el.textContent,
                canEdit: el.getAttribute('data-can-edit') === 'true',
                isFocused: el.getAttribute('data-is-focused') === 'true'
            })),
        select_elements: Array.from(result.querySelectorAll('select[data-semantic-id]'))
            .map(el => ({
                id: el.getAttribute('data-semantic-id'),
                value: el.value,
                selectedIndex: el.selectedIndex,
                multiple: el.multiple,
                selectedValues: Array.from(el.selectedOptions).map(opt => opt.value)
            })),
    };
}

parse();
