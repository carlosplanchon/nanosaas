// The API key page: the account's keys, a new one, and the webhook
// (web/templates/api_key_page.html).

// The ROOT_PATH of the app's URLs, like "/nanosaas", from the page's <html>.
const ROOT_PREFIX = document.documentElement.dataset.rootPrefix;

// When the session expires, go back to the login page.
function sessionExpired(response) {
    if (response.status !== 401) {
        return false;
    }
    window.location.href = `${ROOT_PREFIX}/`;
    return true;
}

// The API keys: their names, the start of each one, and when it was created and last used.
const apiKeysBody = document.getElementById('api-keys');
const noApiKeys = document.getElementById('no-api-keys');
const apiKeyNameInput = document.getElementById('api_key_name');
const newApiKey = document.getElementById('new-api-key');
const newApiKeyName = document.getElementById('new-api-key-name');
const newApiKeyInput = document.getElementById('new_api_key');
const apiKeyMessage = document.getElementById('api-key-message');
const apiKeyError = document.getElementById('api-key-error');

// A row of the keys. As text: their names are the user's.
function apiKeyRow(apiKey) {
    const row = document.createElement('tr');
    const texts = [
        apiKey.name, `${apiKey.prefix}…`, new Date(apiKey.created_at).toLocaleString(),
        apiKey.last_used_at ? new Date(apiKey.last_used_at).toLocaleString() : 'Never',
    ];
    for (const text of texts) {
        const cell = document.createElement('td');
        cell.className = 'px-2 py-1 border-t border-gray-200';
        cell.textContent = text;
        row.append(cell);
    }
    row.children[1].classList.add('font-mono');
    const revoke = document.createElement('button');
    revoke.type = 'button';
    revoke.className = 'text-red-600 hover:underline';
    revoke.textContent = 'Revoke';
    revoke.addEventListener('click', () => revokeApiKey(apiKey));
    const cell = document.createElement('td');
    cell.className = 'px-2 py-1 border-t border-gray-200 text-right';
    cell.append(revoke);
    row.append(cell);
    return row;
}

function showApiKeys(apiKeys) {
    apiKeysBody.replaceChildren(...apiKeys.map(apiKeyRow));
    noApiKeys.classList.toggle('hidden', apiKeys.length > 0);
}

async function loadApiKeys() {
    try {
        const response = await fetch(`${ROOT_PREFIX}/user/api_keys`);
        if (sessionExpired(response)) return;
        if (!response.ok) {
            throw new Error('Failed to fetch the API keys.');
        }
        showApiKeys((await response.json()).api_keys);
    } catch (error) {
        console.error(error);
    }
}

// A change to the keys: shows the keys it answers, and the message, or what went wrong.
// Returns its answer, or null.
async function changeApiKeys(method, path, body, message) {
    apiKeyMessage.classList.add('hidden');
    apiKeyError.classList.add('hidden');
    try {
        const response = await fetch(`${ROOT_PREFIX}/user/api_keys${path}`, {
            method,
            headers: body ? { 'Content-Type': 'application/json' } : {},
            body: body ? JSON.stringify(body) : undefined
        });
        if (sessionExpired(response)) return null;

        const data = await response.json();
        if (!response.ok) {
            // The server says what's wrong, like a missing name.
            throw new Error(typeof data.detail === 'string' ? data.detail : 'Check the name.');
        }
        showApiKeys(data.api_keys);
        apiKeyMessage.textContent = message;
        apiKeyMessage.classList.toggle('hidden', !message);
        return data;
    } catch (error) {
        apiKeyError.textContent = error.message;
        apiKeyError.classList.remove('hidden');
        return null;
    }
}

document.getElementById('api-key-form').addEventListener('submit', async (event) => {
    event.preventDefault();
    const name = apiKeyNameInput.value.trim();
    const data = await changeApiKeys('POST', '', { name }, '');
    if (data) {
        // The only time it's shown: only its hash is stored.
        newApiKeyName.textContent = name;
        newApiKeyInput.value = data.api_key;
        newApiKey.classList.remove('hidden');
        apiKeyNameInput.value = '';
        newApiKeyInput.select();
    }
});

async function revokeApiKey(apiKey) {
    if (!confirm(`Revoke the key "${apiKey.name}"? The requests with it will get a 401 error from now on.`)) {
        return;
    }
    if (await changeApiKeys('DELETE', `/${apiKey.id}`, null, `Revoked "${apiKey.name}".`)
            && newApiKeyInput.value.startsWith(apiKey.prefix)) {
        // The new key that the page shows: it doesn't work anymore.
        newApiKey.classList.add('hidden');
        newApiKeyInput.value = '';
    }
}

// The webhook: where the account gets an event when each of its tasks ends.
const webhookUrlInput = document.getElementById('webhook_url');
const webhookSecretInput = document.getElementById('webhook_secret');
const webhookDetails = document.getElementById('webhook-details');
const webhookRemoveButton = document.getElementById('webhook-remove');
const webhookDisabled = document.getElementById('webhook-disabled');
const webhookDisabledAt = document.getElementById('webhook-disabled-at');
const webhookDisabledReason = document.getElementById('webhook-disabled-reason');
const webhookTestButton = document.getElementById('webhook-test');
const webhookResendButton = document.getElementById('webhook-resend');
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
    // When the worker disabled it, and why. As text: the reason has the receiver's answer.
    webhookDisabled.classList.toggle('hidden', !data.disabled_at);
    webhookDisabledAt.textContent = data.disabled_at ? `on ${new Date(data.disabled_at).toLocaleString()}` : '';
    webhookDisabledReason.textContent = data.disabled_reason ?? '';
    // Meanwhile, the events would wait: the server sends neither.
    webhookTestButton.classList.toggle('hidden', Boolean(data.disabled_at));
    webhookResendButton.classList.toggle('hidden', Boolean(data.disabled_at) || !data.failed);
    webhookResendButton.textContent = `Resend the failed events (${data.failed})`;
    webhookDeliveries.replaceChildren(...data.deliveries.map(deliveryRow));
    webhookNoDeliveries.classList.toggle('hidden', data.deliveries.length > 0);
    clearTimeout(webhookRefresh);
    // Not while it's disabled: its pending events wait.
    if (document.hidden || data.disabled_at || !data.deliveries.some(delivery => delivery.status === 'PENDING')) {
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

webhookTestButton.addEventListener('click', () => changeWebhook(
    'POST', '/test', null, 'Test event queued: it shows below, with how it went.'
));

document.getElementById('webhook-enable').addEventListener('click', () => changeWebhook(
    'POST', '/enable', null, 'Enabled: the events that waited go out now.'
));

webhookResendButton.addEventListener('click', () => changeWebhook(
    'POST', '/resend', null, 'The failed events go out again, with their IDs: they show below, with how they go.'
));

// Initialize page
loadApiKeys();
loadWebhook();
