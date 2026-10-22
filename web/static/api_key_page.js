// The API key page: the start of the current key, a new one, and the webhook
// (web/templates/api_key_page.html).

// The ROOT_PATH of the app's URLs, like "/nanosaas", from the page's <html>.
const ROOT_PREFIX = document.documentElement.dataset.rootPrefix;

const apiKeyInput = document.getElementById('api_key');
const successMessage = document.getElementById('success-message');
const errorMessage = document.getElementById('error-message');

// When the session expires, go back to the login page.
function sessionExpired(response) {
    if (response.status !== 401) {
        return false;
    }
    window.location.href = `${ROOT_PREFIX}/`;
    return true;
}

// Fetch the start of the current API key on page load (only its
// hash is stored, so the whole key can't be shown again)
async function fetchApiKey() {
    try {
        const response = await fetch(`${ROOT_PREFIX}/user/api_key`, {
            method: 'GET',
            credentials: 'include'
        });
        if (sessionExpired(response)) return;

        if (!response.ok) {
            throw new Error('Failed to fetch API key.');
        }

        const data = await response.json();
        apiKeyInput.value = data.api_key_prefix
            ? `${data.api_key_prefix}…`
            : 'No API key generated yet';
    } catch (error) {
        apiKeyInput.value = 'Error fetching API key.';
        console.error(error);
    }
}

// Generate a new API key
document.getElementById('generate-key-button').addEventListener('click', async () => {
    successMessage.classList.add('hidden');
    errorMessage.classList.add('hidden');

    try {
        const response = await fetch(`${ROOT_PREFIX}/user/api_key`, {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json'
            },
            credentials: 'include'
        });
        if (sessionExpired(response)) return;

        if (!response.ok) {
            throw new Error(`Error: ${response.statusText}`);
        }

        const data = await response.json();
        apiKeyInput.value = data.api_key;
        successMessage.textContent = "API Key generated. Copy it now: it won't be shown again.";
        successMessage.classList.remove('hidden');
    } catch (error) {
        errorMessage.textContent = error.message;
        errorMessage.classList.remove('hidden');
    }
});

// The webhook: where the account gets an event when each of its tasks ends.
const webhookUrlInput = document.getElementById('webhook_url');
const webhookSecretInput = document.getElementById('webhook_secret');
const webhookDetails = document.getElementById('webhook-details');
const webhookRemoveButton = document.getElementById('webhook-remove');
const webhookDeliveries = document.getElementById('webhook-deliveries');
const webhookNoDeliveries = document.getElementById('webhook-no-deliveries');
const webhookMessage = document.getElementById('webhook-message');
const webhookError = document.getElementById('webhook-error');
// While an event is pending, the page checks how it went: after 3 seconds, and
// then less often, up to every minute, since its next attempt may be hours away.
let webhookRefresh = null;
let webhookDelay = 3000;

function deliveryStatus(delivery) {
    const attempts = `${delivery.attempts} attempt${delivery.attempts === 1 ? '' : 's'}`;
    if (delivery.status === 'DELIVERED') return `Delivered (${attempts})`;
    if (delivery.status === 'FAILED') return `Failed (${attempts})`;
    return delivery.attempts ? `Pending (${attempts})` : 'Pending';
}

// A row of the latest events. As text: the receivers' answers come from other servers.
function deliveryRow(delivery) {
    const row = document.createElement('tr');
    const texts = [
        new Date(delivery.created_at).toLocaleString(), delivery.type, delivery.task_id ?? '',
        deliveryStatus(delivery), delivery.last_response ?? '',
    ];
    for (const text of texts) {
        const cell = document.createElement('td');
        cell.className = 'px-2 py-1 border-t border-gray-200';
        cell.textContent = text;
        row.append(cell);
    }
    return row;
}

function showWebhook(data) {
    // Not over a URL being typed.
    if (document.activeElement !== webhookUrlInput) {
        webhookUrlInput.value = data.url ?? '';
    }
    webhookSecretInput.value = data.secret ?? '';
    webhookDetails.classList.toggle('hidden', !data.url);
    webhookRemoveButton.classList.toggle('hidden', !data.url);
    webhookDeliveries.replaceChildren(...data.deliveries.map(deliveryRow));
    webhookNoDeliveries.classList.toggle('hidden', data.deliveries.length > 0);
    clearTimeout(webhookRefresh);
    if (document.hidden || !data.deliveries.some(delivery => delivery.status === 'PENDING')) {
        webhookDelay = 3000;
        return;
    }
    webhookRefresh = setTimeout(loadWebhook, webhookDelay);
    webhookDelay = Math.min(webhookDelay * 2, 60000);
}

// Not while the tab is hidden: it catches up when it shows again.
document.addEventListener('visibilitychange', () => {
    if (document.hidden) {
        clearTimeout(webhookRefresh);
    } else {
        loadWebhook();
    }
});

async function loadWebhook() {
    try {
        const response = await fetch(`${ROOT_PREFIX}/user/webhook`);
        if (sessionExpired(response)) return;
        if (!response.ok) {
            throw new Error('Failed to fetch the webhook.');
        }
        showWebhook(await response.json());
    } catch (error) {
        console.error(error);
    }
}

// A change to the webhook: shows the webhook it answers, and the message, or what went wrong.
async function changeWebhook(method, path, body, message) {
    webhookMessage.classList.add('hidden');
    webhookError.classList.add('hidden');
    try {
        const response = await fetch(`${ROOT_PREFIX}/user/webhook${path}`, {
            method,
            headers: body ? { 'Content-Type': 'application/json' } : {},
            body: body ? JSON.stringify(body) : undefined
        });
        if (sessionExpired(response)) return;

        const data = await response.json();
        if (!response.ok) {
            // The server says what's wrong with the URL.
            throw new Error(typeof data.detail === 'string' ? data.detail : 'Check the URL.');
        }
        webhookDelay = 3000;
        showWebhook(data);
        webhookMessage.textContent = message;
        webhookMessage.classList.remove('hidden');
    } catch (error) {
        webhookError.textContent = error.message;
        webhookError.classList.remove('hidden');
    }
}

document.getElementById('webhook-save').addEventListener('click', () => changeWebhook(
    'PUT', '', { url: webhookUrlInput.value.trim() },
    'Saved: the tasks that end from now on send their events there.'
));

webhookRemoveButton.addEventListener('click', () => {
    if (confirm("Remove the webhook? The events that weren't sent yet won't be.")) {
        changeWebhook('DELETE', '', null, 'Removed.');
    }
});

document.getElementById('webhook-new-secret').addEventListener('click', () => {
    if (confirm('Generate a new secret? It signs the events from now on: your receiver needs it to check them.')) {
        changeWebhook('POST', '/secret', null, 'New secret generated.');
    }
});

document.getElementById('webhook-test').addEventListener('click', () => changeWebhook(
    'POST', '/test', null, 'Test event queued: it shows below, with how it went.'
));

// Initialize page
fetchApiKey();
loadWebhook();
