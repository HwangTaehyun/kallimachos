import { describe, it, expect, vi, afterEach } from 'vitest';
import { render, screen, cleanup, fireEvent } from '@testing-library/react';
import { SettingsShell } from '../src/components/SettingsShell';

afterEach(cleanup);

const shell = (props: Partial<Parameters<typeof SettingsShell>[0]> = {}) =>
	render(
		<SettingsShell active="pipeline" onNav={() => {}} onClose={() => {}} {...props}>
			<p>the content</p>
		</SettingsShell>,
	);

describe('SettingsShell — the shortcuts', () => {
	it('Esc closes it', () => {
		const onClose = vi.fn();
		shell({ onClose });
		fireEvent.keyDown(window, { key: 'Escape' });
		expect(onClose).toHaveBeenCalledTimes(1);
	});

	it('Esc inside an input **does not close** and only leaves that field', () => {
		//  Meaning to cancel a value must not become closing the screen.
		const onClose = vi.fn();
		shell({ onClose });
		const search = screen.getByLabelText('Search settings');
		search.focus();
		fireEvent.keyDown(search, { key: 'Escape' });
		expect(onClose).not.toHaveBeenCalled();
		expect(document.activeElement).not.toBe(search);
	});

	it('⌘F goes to the search', () => {
		shell();
		const search = screen.getByLabelText('Search settings');
		expect(document.activeElement).not.toBe(search);
		fireEvent.keyDown(window, { key: 'f', metaKey: true });
		expect(document.activeElement).toBe(search);
	});

	it('Ctrl+F does the same (off a Mac)', () => {
		shell();
		fireEvent.keyDown(window, { key: 'f', ctrlKey: true });
		expect(document.activeElement).toBe(screen.getByLabelText('Search settings'));
	});

	it('a plain f does nothing', () => {
		//  Without checking the modifier, every letter typed jumps to the search.
		shell();
		fireEvent.keyDown(window, { key: 'f' });
		expect(document.activeElement).not.toBe(screen.getByLabelText('Search settings'));
	});

	it('it does not listen for shortcuts while hidden', () => {
		//  Settings stays mounted behind the galaxy while hidden (to protect the edit buffers).
		//  Esc acting then would jump the route while the graph is being viewed.
		const onClose = vi.fn();
		shell({ onClose, hidden: true });
		fireEvent.keyDown(window, { key: 'Escape' });
		expect(onClose).not.toHaveBeenCalled();
	});
});

describe('SettingsShell — the contents list', () => {
	it('a search narrows the sidebar', () => {
		shell();
		expect(screen.getByRole('button', { name: 'Pipeline' })).toBeTruthy();
		fireEvent.change(screen.getByLabelText('Search settings'), { target: { value: 'bm25' } });
		expect(screen.queryByRole('button', { name: 'Pipeline' })).toBeNull();
		expect(screen.getByRole('button', { name: 'Chunking · Search' })).toBeTruthy();
	});

	it('with no match it echoes the query back', () => {
		shell();
		fireEvent.change(screen.getByLabelText('Search settings'), { target: { value: 'zzz' } });
		expect(screen.getByText(/No settings match "zzz"/)).toBeTruthy();
	});

	it('filtering leaves the content currently being viewed alone', () => {
		//  Search is for **finding**, not for discarding the current screen.
		shell();
		fireEvent.change(screen.getByLabelText('Search settings'), { target: { value: 'bm25' } });
		expect(screen.getByText('the content')).toBeTruthy();
		expect(screen.getByRole('heading', { name: 'Pipeline' })).toBeTruthy();
	});

	it('it reports an item being pressed', () => {
		const onNav = vi.fn();
		shell({ onNav });
		fireEvent.click(screen.getByRole('button', { name: 'Paths' }));
		expect(onNav).toHaveBeenCalledWith('paths');
	});

	it('the current item carries aria-current', () => {
		shell({ active: 'paths' });
		expect(screen.getByRole('button', { name: 'Paths' }).getAttribute('aria-current')).toBe('page');
		expect(screen.getByRole('button', { name: 'Status' }).getAttribute('aria-current')).toBeNull();
	});

	it('"back to the app" closes it', () => {
		const onClose = vi.fn();
		shell({ onClose });
		fireEvent.click(screen.getByRole('button', { name: /Back to app/ }));
		expect(onClose).toHaveBeenCalledTimes(1);
	});
});
