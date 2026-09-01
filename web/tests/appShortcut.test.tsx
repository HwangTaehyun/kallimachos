import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, cleanup, fireEvent, act } from '@testing-library/react';

//  The galaxy starts WebGL —— jsdom has none.  Settings calls the API.
//  What this looks at is **routing and ⌘,** alone, so both are stood up as stand-ins.
vi.mock('../src/components/GalaxyView', () => ({
	GalaxyView: ({ hidden }: { hidden: boolean }) => <div data-testid="galaxy" data-hidden={String(hidden)} />,
}));
vi.mock('../src/components/SettingsPage', () => ({
	SettingsPage: ({ hidden, onClose }: { hidden: boolean; onClose: () => void }) => (
		<div data-testid="settings" data-hidden={String(hidden)}>
			<button type="button" onClick={onClose}>Close</button>
		</div>
	),
}));

beforeEach(() => { window.location.hash = ''; });
afterEach(cleanup);

const load = async () => (await import('../src/App')).default;

const press = (key: string, mod: 'meta' | 'ctrl' | 'none' = 'meta') =>
	act(() => {
		fireEvent.keyDown(window, { key, metaKey: mod === 'meta', ctrlKey: mod === 'ctrl' });
		//  A hash change comes back as a hashchange event.  jsdom does not fire it synchronously, so
		//  **it has to be fired here.**
		//  ⚠ The "a comma with no modifier" test used to do a keyDown without this line.  Then the
		//    handler changing the hash caused no re-render, `everOpened` stayed as it was, and
		//    **the test passed identically whether or not it was blocked**.  A mutation deleting the
		//    modifier check really was missed (2026-08-23).
		window.dispatchEvent(new HashChangeEvent('hashchange'));
	});

describe('App — ⌘, opens settings', () => {
	it('settings is not even mounted at first', async () => {
		const App = await load();
		render(<App />);
		//  At boot, `api.llm()` starts the claude CLI.  Not a price to charge someone who came only
		//  to look at the galaxy.
		expect(screen.queryByTestId('settings')).toBeNull();
	});

	it('⌘, opens it and pressing again closes it', async () => {
		const App = await load();
		render(<App />);
		press(',');
		expect(screen.getByTestId('settings').dataset.hidden).toBe('false');
		expect(screen.getByTestId('galaxy').dataset.hidden).toBe('true');

		press(',');
		//  ★ Closing **does not unmount** —— it protects the YAML and draft being edited.
		expect(screen.getByTestId('settings').dataset.hidden).toBe('true');
		expect(screen.getByTestId('galaxy').dataset.hidden).toBe('false');
	});

	it('Ctrl+, does the same', async () => {
		const App = await load();
		render(<App />);
		press(',', 'ctrl');
		expect(screen.getByTestId('settings').dataset.hidden).toBe('false');
	});

	it('a comma with no modifier does nothing', async () => {
		//  Settings must not open every time a comma is typed while writing.
		const App = await load();
		render(<App />);
		press(',', 'none');
		expect(screen.queryByTestId('settings')).toBeNull();
	});

	it('the header’s settings button opens it too', async () => {
		const App = await load();
		render(<App />);
		await act(async () => { fireEvent.click(screen.getByRole('button', { name: /Settings/ })); });
		act(() => { window.dispatchEvent(new HashChangeEvent('hashchange')); });
		expect(screen.getByTestId('settings').dataset.hidden).toBe('false');
	});
});

describe('App — the way out of settings', () => {
	it('the header button names **where it goes, not where you are**', async () => {
		//  A "Settings" button visible while in settings gives no idea what it does.
		const App = await load();
		render(<App />);
		expect(screen.getByRole('button', { name: /Settings/ })).toBeTruthy();
		expect(screen.queryByRole('button', { name: /^Graph/ })).toBeNull();

		press(',');
		expect(screen.getByRole('button', { name: /Graph\s*Esc/ })).toBeTruthy();
	});

	it('pressing the wordmark returns to the graph', async () => {
		//  Logo → home is an idiom needing no explanation.  With the sidebar the only way out,
		//  anyone who does not find that one place is trapped.
		const App = await load();
		render(<App />);
		press(',');
		expect(screen.getByTestId('settings').dataset.hidden).toBe('false');

		await act(async () => { fireEvent.click(screen.getByRole('button', { name: /Kallimachos/ })); });
		act(() => { window.dispatchEvent(new HashChangeEvent('hashchange')); });
		expect(screen.getByTestId('galaxy').dataset.hidden).toBe('false');
	});

	it('what follows the wordmark says where you are', async () => {
		const App = await load();
		render(<App />);
		expect(screen.getByRole('button', { name: /Kallimachos · Knowledge graph/ })).toBeTruthy();
		press(',');
		expect(screen.getByRole('button', { name: /Kallimachos · Settings/ })).toBeTruthy();
	});

	it('the header button gets out too', async () => {
		const App = await load();
		render(<App />);
		press(',');
		await act(async () => { fireEvent.click(screen.getByRole('button', { name: /^Graph/ })); });
		act(() => { window.dispatchEvent(new HashChangeEvent('hashchange')); });
		expect(screen.getByTestId('galaxy').dataset.hidden).toBe('false');
	});

	it('entering directly at #/settings starts in settings', async () => {
		window.location.hash = '#/settings';
		const App = await load();
		render(<App />);
		expect(screen.getByTestId('settings').dataset.hidden).toBe('false');
	});
});
