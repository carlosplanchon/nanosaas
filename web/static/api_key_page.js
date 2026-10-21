// The API key page: the start of the current key, and a new one (web/templates/api_key_page.html).

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

// Initialize page
fetchApiKey();
