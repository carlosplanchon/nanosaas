// The admin page: the accounts, to give them credits or delete them (web/templates/admin.html).

// The ROOT_PATH of the app's URLs, like "/nanosaas", from the page's <html>.
const ROOT_PREFIX = document.documentElement.dataset.rootPrefix;

function accountManager() {
    return {
        search: '',
        accounts: [],
        // The credits typed for each account, by its ID.
        adding: {},

        init() {
            this.fetchAccounts();
        },

        // For the template, whose expressions can't create a Date: Alpine's CSP build
        // doesn't run any JavaScript but the component's methods.
        formatDate(value) {
            return new Date(value).toLocaleString();
        },

        // The latest accounts, or the ones with the search in their email.
        async fetchAccounts() {
            const response = await fetch(`${ROOT_PREFIX}/admin/accounts?search=${encodeURIComponent(this.search)}`);
            if (!response.ok) {
                // Without an admin's session the admin routes answer 404: to the login page.
                window.location.href = `${ROOT_PREFIX}/`;
                return;
            }
            this.accounts = await response.json();
        },

        async addCredits(account) {
            const response = await fetch(`${ROOT_PREFIX}/admin/accounts/${account.id}/credits`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ credits: Number(this.adding[account.id]) })
            });
            if (response.ok) {
                account.credits = (await response.json()).credits;
                this.adding[account.id] = '';
            } else if (response.status === 422) {
                alert('Add 1 credit or more.');
            } else {
                // The account is gone, or the admin's session ended: the list tells which.
                await this.fetchAccounts();
            }
        },

        async deleteAccount(account) {
            if (!confirm(`Delete the account of ${account.email}, with its tasks? This cannot be undone.`)) return;
            await fetch(`${ROOT_PREFIX}/admin/accounts/${account.id}`, { method: 'DELETE' });
            // Deleted now or before, or the admin's session ended: the list tells which.
            await this.fetchAccounts();
        }
    };
}

// Alpine.js, which loads after this script, finds the component by its name.
document.addEventListener('alpine:init', () => {
    Alpine.data('accountManager', accountManager);
});
