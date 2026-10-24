// The admin page: the accounts, to see their histories, give them credits or delete
// them (web/templates/admin.html).

// The ROOT_PATH of the app's URLs, like "/nanosaas", from the page's <html>.
const ROOT_PREFIX = document.documentElement.dataset.rootPrefix;

function accountManager() {
    return {
        search: '',
        accounts: [],
        // The credits typed for each account, by its ID.
        adding: {},
        // The account whose history shows, or null. The pages of its tasks and of its
        // credit history shown, like the user panel's: as the task or the movement
        // that its rows come before (null for the first page), after the newer pages'.
        viewing: null,
        tasks: [],
        taskPages: [null],
        moreTasks: false,
        movements: [],
        movementPages: [null],
        moreMovements: false,

        init() {
            this.fetchAccounts();
        },

        // For the template, whose expressions can't create a Date: Alpine's CSP build
        // doesn't run any JavaScript but the component's methods. The histories' are
        // in history.js.
        formatDate(value) {
            return new Date(value).toLocaleString();
        },
        formatParameters,
        showResult,
        formatAmount,
        reasonText,

        async viewHistory(account) {
            this.viewing = account;
            this.taskPages = [null];
            this.movementPages = [null];
            this.tasks = [];
            this.movements = [];
            await Promise.all([this.fetchTasks(), this.fetchMovements()]);
        },

        closeHistory() {
            this.viewing = null;
        },

        // A page of the account's history, or null if another page or account was chosen
        // meanwhile. When the account is gone, or the admin's session ended, the history
        // closes, and the list tells which.
        async fetchPage(history, pages) {
            const account = this.viewing;
            const before = pages.at(-1);
            const query = before ? `?before=${encodeURIComponent(before)}` : '';
            const response = await fetch(`${ROOT_PREFIX}/admin/accounts/${account.id}/${history}${query}`);
            if (account !== this.viewing || before !== pages.at(-1)) return null;
            if (!response.ok) {
                this.viewing = null;
                await this.fetchAccounts();
                return null;
            }
            return await response.json();
        },

        async fetchTasks() {
            const data = await this.fetchPage('task_history', this.taskPages);
            if (data) {
                this.tasks = withLocalDates(data.tasks);
                this.moreTasks = data.has_more;
            }
        },

        olderTasks() {
            this.taskPages.push(this.tasks.at(-1).task_id);
            this.fetchTasks();
        },

        newerTasks() {
            this.taskPages.pop();
            this.fetchTasks();
        },

        async fetchMovements() {
            const data = await this.fetchPage('credit_history', this.movementPages);
            if (data) {
                this.movements = withLocalDates(data.movements);
                this.moreMovements = data.has_more;
            }
        },

        olderMovements() {
            this.movementPages.push(this.movements.at(-1).id);
            this.fetchMovements();
        },

        newerMovements() {
            this.movementPages.pop();
            this.fetchMovements();
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
                // Its history, if it shows, has the credits now.
                if (this.viewing?.id === account.id) {
                    this.movementPages = [null];
                    await this.fetchMovements();
                }
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
            if (this.viewing?.id === account.id) {
                this.viewing = null;
            }
            // Deleted now or before, or the admin's session ended: the list tells which.
            await this.fetchAccounts();
        }
    };
}

// Alpine.js, which loads after this script, finds the component by its name.
document.addEventListener('alpine:init', () => {
    Alpine.data('accountManager', accountManager);
});
