// An account's histories, as the user panel and the admin page show them: its
// tasks, with their parameters and results, and the changes of its credits.
// The pages load json.js before it, for the results' colors.

// A task's parameters, like "x: 6, y: 3".
function formatParameters(parameters) {
    return Object.entries(parameters ?? {}).map(([name, value]) => `${name}: ${JSON.stringify(value)}`).join(', ');
}

// A task's result in the element: its JSON, with its colors, once it ended,
// as SUCCESS or FAILURE, and a dash before, or if it was canceled. A task
// that returned None has null. For x-effect: Alpine's CSP build has no x-html.
function showResult(element, task) {
    if (['SUCCESS', 'FAILURE'].includes(task.status)) {
        element.innerHTML = highlightJson(task.result);
    } else {
        element.textContent = '—';
    }
}

// Why the credits changed, by the movement's reason.
const REASONS = {
    initial: 'Initial credits',
    task: 'Cost of the task',
    refund: 'Given back',
    admin: 'Added by an admin',
    script: 'Added',
    opening: 'Balance when the history began',
};

function reasonText(reason) {
    return REASONS[reason] ?? reason;
}

// A change of the credits, like "+25" or "-1".
function formatAmount(amount) {
    return amount > 0 ? `+${amount}` : String(amount);
}

// The rows of a page of a history, with their dates as the browser shows them.
function withLocalDates(rows) {
    return rows.map(row => ({ ...row, created_at: new Date(row.created_at).toLocaleString() }));
}
