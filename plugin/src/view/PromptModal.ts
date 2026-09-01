import { Modal } from 'obsidian';
import type { App } from 'obsidian';
import { t } from '../i18n';

export interface PromptOpts {
	title: string;
	/** Given, an input appears and its value goes to onSubmit.  Without it, this is a confirm-only dialog. */
	value?: string;
	/** Supporting text under the title.  If the action cannot be undone, say so here. */
	body?: string;
	/** The confirm button's wording */
	cta: string;
	/** A destructive action turns the confirm button red — it has to look different from cancel for mispresses to fall */
	danger?: boolean;
}

/**
 * A small dialog serving both name entry and confirmation of an action that cannot be undone.
 *
 * It uses Modal, contentEl and open/close only.  All four exist in both the web shim
 * (`src/web/obsidian.ts`) and the real Obsidian API, so one implementation covers both environments.
 * The input and confirm forms are one class because the only difference between them is "is there
 * an input", and splitting the class would mean two copies of the button row, key handling and closing rules.
 */
export class PromptModal extends Modal {
	constructor(
		app: App,
		private opts: PromptOpts,
		private onSubmit: (value: string) => void,
	) {
		super(app);
	}

	onOpen(): void {
		const { contentEl, opts } = this;
		contentEl.addClass('gx-prompt');
		contentEl.createDiv({ cls: 'gx-prompt-title', text: opts.title });
		if (opts.body) contentEl.createDiv({ cls: 'gx-prompt-body', text: opts.body });

		const input =
			opts.value === undefined
				? null
				: contentEl.createEl('input', { cls: 'gx-prompt-input', value: opts.value });

		const submit = (): void => {
			const v = input ? input.value.trim() : '';
			// An empty name is not saved.  Closing would lose what the user typed, so it stays in the input.
			if (input && !v) {
				input.focus();
				return;
			}
			this.close();
			this.onSubmit(v);
		};

		if (input) {
			input.addEventListener('keydown', (e) => {
				if (e.key === 'Enter') {
					e.preventDefault();
					submit();
				}
			});
		}

		const row = contentEl.createDiv({ cls: 'gx-prompt-row' });
		const cancel = row.createEl('button', { cls: 'gx-prompt-cancel', text: t('mine.cancel') });
		cancel.addEventListener('click', () => this.close());
		const ok = row.createEl('button', { cls: 'gx-prompt-ok', text: opts.cta });
		if (opts.danger) ok.addClass('is-danger');
		ok.addEventListener('click', submit);

		// Placed where the hand already is on opening.  The name is there to be edited, so it is selected whole too.
		// Only a destructive action focuses cancel —— this dialog exists to catch a mispress, and
		// if one more Enter deletes anyway, putting it up meant nothing.
		// setTimeout 0 — the shim calls onOpen() inside open(), so it is not attached to the DOM yet.
		window.setTimeout(() => {
			if (input) {
				input.focus();
				input.select();
			} else if (opts.danger) {
				cancel.focus();
			} else {
				ok.focus();
			}
		}, 0);
	}

	onClose(): void {
		this.contentEl.empty();
	}
}
