// highlightJson(value): the value as JSON, in HTML, with its keys, strings,
// numbers, and true, false and null in spans of Tailwind colors. The pages put
// it in an element's innerHTML with showResult(), from x-effect: Alpine's CSP
// build has no x-html.
// scripts/build_css.py reads this file too, for these classes.

// The span of each kind of token.
const JSON_SPANS = {
    key: '<span class="text-purple-700">',
    string: '<span class="text-green-700">',
    number: '<span class="text-blue-700">',
    literal: '<span class="text-orange-700">',
};

// A string, with the colon after it if it's a key; true, false or null; or a number.
const JSON_TOKEN = /("(?:\\.|[^\\"])*")(\s*:)?|\b(?:true|false|null)\b|-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?/g;

function highlightJson(value) {
    // Escaped first, so the only tags are the spans.
    const json = JSON.stringify(value ?? null, null, 2)
        .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
    return json.replace(JSON_TOKEN, (token, string, colon) => {
        const kind = string ? (colon ? 'key' : 'string') : /\d/.test(token) ? 'number' : 'literal';
        return JSON_SPANS[kind] + (string ?? token) + '</span>' + (colon ?? '');
    });
}
