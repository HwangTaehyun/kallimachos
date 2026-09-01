import { useEffect, useState } from 'react';
import { api, type ConfigSetting, type ConfigView, type Status } from '../api';
import { useT } from '../i18n/lang';
import S from '../i18n/strings/configPanel';
import { LoadingBlock } from './Loading';
import { SettingGroup, SettingRow } from './SettingRow';

/**
 * Settings —— **what is read from where and written to where**, and how to change it.
 *
 * Two kinds are split into **different pages**.  Mixing them makes it a lie:
 *
 *   paths (vault · LanceDB)   read-only.  They are bind mounts in a container and cannot be
 *                             changed at runtime —— changing the value alone leaves a path that
 *                             does not exist.  So there is no input; it says **how to change it**.
 *
 *   tuning (chunking, BM25 …)  changeable.  But most are **already baked into the DB**, so a
 *                             re-index is needed after changing them.  That fact is shown as a
 *                             chip, and a banner appears when the state diverges from the DB.
 *
 * A value an environment variable won.  **is locked.**  The priority is env > config.json > the
 * defaults, and without locking it becomes "an input you change and nothing happens" —— worse than none.
 *
 * Why one component serves two pages: both come out of **the same single `/api/config`**.
 * A component per page would pay 1.5 seconds (Python + LanceDB) again on every move between them.
 * And the draft has to live here so a quick look at "Paths" does not lose what was typed.
 */
export function ConfigPanel({ onReindex, busy, reloadKey = 0, show = 'tuning', status = null }: {
	onReindex: () => void;
	busy: boolean;
	reloadKey?: number;
	show?: 'tuning' | 'paths';
	/** Received to compare the DB's origin —— `meta.vault_path` exists only here. */
	status?: Status | null;
}) {
	const t = useT(S);
	const [cfg, setCfg] = useState<ConfigView | null>(null);
	const [draft, setDraft] = useState<Record<string, string>>({});
	const [err, setErr] = useState<string | null>(null);
	const [saving, setSaving] = useState(false);
	const [saved, setSaved] = useState(false);

	const load = () => {
		//  ⚠ `setDraft({})` here means **a late response erases what the user typed.**
		//    Measured (2026-08-22, in a browser): StrictMode runs the effect twice in development
		//    and `/api/config` takes 1.5 seconds because of Python + LanceDB.  Editing three
		//    fields in that window put **all three back to their original values** and locked the
		//    save button again.  No error at all —— what was typed simply disappears.
		//
		//    The same shape as polling overriding the user's run choice in SettingsPage.
		//    The draft is **what a person typed**, so a server response does not touch it.  It is
		//    cleared only on a successful save and on pressing "revert".
		api.config()
			.then((c) => { setCfg(c); setErr(null); })
			.catch((e) => setErr(e instanceof Error ? e.message : String(e)));
	};
	useEffect(load, [reloadKey]);

	if (err && !cfg) return <p className="text-sm text-crit">{err}</p>;
	if (!cfg) return <LoadingBlock label={t('loadingSettings')} />;

	const editable = cfg.settings.filter((s) => s.source !== 'env');
	const dirty = Object.entries(draft).filter(([k, v]) => {
		const s = cfg.settings.find((x) => x.key === k);
		return s && v !== String(s.value);
	});

	const save = async () => {
		setSaving(true);
		try {
			const patch: Record<string, string | number> = {};
			for (const [k, v] of dirty) patch[k] = v;
			setCfg(await api.saveConfig(patch));
			setDraft({});
			setErr(null);
			setSaved(true);
			setTimeout(() => setSaved(false), 3000);
		} catch (e) {
			setErr(e instanceof Error ? e.message : String(e));
		} finally {
			setSaving(false);
		}
	};

	const problem = cfg.error && (
		<p role="alert" className="rounded-card border border-red-500/40 bg-red-950/40 px-4 py-3 text-sm text-crit">
			{cfg.error}
		</p>
	);

	if (show === 'paths') {
		return (
			<div className="flex flex-col gap-6">
				{problem}
				<SettingGroup
					desc={cfg.in_container ? t('cantChangeContainer') : t('cantChangeHere')}
					footer={
						<p className="mt-1 text-xs leading-relaxed text-ink-400">
							<span className="text-ink-200">{t('toChange')}</span> — {cfg.paths[0]?.how}
						</p>
					}
				>
					{cfg.paths.map((p) => (
						<SettingRow
							key={p.key}
							label={p.label}
							badges={
								<>
									<Source source={p.source} envName={p.env} />
									{!p.exists && (
										<span className="rounded bg-red-500/20 px-1.5 py-0.5 text-2xs font-medium text-crit">
											{t('folderMissing')}
										</span>
									)}
								</>
							}
							desc={p.source === 'default' ? t('pathDefault')(p.env) : t('pathSetByEnv')(p.env)}
						>
							<code className="max-w-full break-all rounded-control border border-ink-800 bg-ink-950
							                 px-2.5 py-1 font-mono text-xs text-ink-200 md:max-w-[24rem]">
								{p.value}
							</code>
						</SettingRow>
					))}
				</SettingGroup>

				{/*  **Which vault this DB was built from.**

				     The list above is "where this server is currently set to read".  But indexing
				     also runs from the CLI —— `KAL_VAULT=/other just run index` rebuilds the same
				     DB **from a different vault**, and the screen keeps showing the old path,
				     knowing nothing.  In that state the "Status" screen reads every document as
				     'deleted' —— with the cause written nowhere on screen.
				     The DB records the path it was built with in meta.  That is compared here. */}
				{status?.db?.vault_path && (
					<SettingGroup desc={t('dbSourceDesc')}>
						<SettingRow
							label={t('vaultBuiltLabel')}
							badges={
								status.db.vault_now && status.db.vault_path !== status.db.vault_now ? (
									<span className="rounded bg-red-500/20 px-1.5 py-0.5 text-2xs font-medium text-crit">
										{t('differsFromCurrent')}
									</span>
								) : (
									<span className="rounded bg-ink-800 px-1.5 py-0.5 text-2xs font-medium text-ink-300">
										{t('match')}
									</span>
								)
							}
							desc={
								status.db.vault_now && status.db.vault_path !== status.db.vault_now
									? t('vaultMismatchDesc')(status.db.vault_now)
									: t('vaultMatchDesc')
							}
						>
							<code className="max-w-full break-all rounded-control border border-ink-800 bg-ink-950
							                 px-2.5 py-1 font-mono text-xs text-ink-200 md:max-w-[24rem]">
								{status.db.vault_path}
							</code>
						</SettingRow>
					</SettingGroup>
				)}
			</div>
		);
	}

	return (
		<div className="flex flex-col gap-6">
			{problem}

			{/* If the DB was built with **different values**, that is the most important fact there is.
			    Change a setting without re-indexing and the screen's values differ from how search actually behaves. */}
			{cfg.drift.length > 0 && (
				<section className="rounded-card border border-amber-500/50 bg-amber-950/25 px-4 py-3">
					<h3 className="text-sm font-semibold text-amber-100">
						{t('driftTitle')}
					</h3>
					<ul className="mt-2 space-y-1 text-sm text-amber-100/90">
						{cfg.drift.map((d) => (
							<li key={d.key} className="flex flex-wrap items-baseline gap-2">
								<code className="rounded bg-black/30 px-1.5 py-0.5 text-2xs">{d.key}</code>
								<span className="tabular-nums">DB {d.db}</span>
								<span aria-hidden className="text-amber-300/60">→</span>
								<span className="font-semibold tabular-nums">{t('settingLabel')} {d.now}</span>
							</li>
						))}
					</ul>
					<button
						type="button"
						onClick={onReindex}
						disabled={busy}
						className="mt-3 rounded-control border border-amber-500/50 px-3 py-1.5 text-xs font-medium
						           text-amber-100 transition-[color,background-color,box-shadow,scale] duration-150
						           enabled:hover:bg-amber-500/15 enabled:active:scale-[0.96]
						           disabled:cursor-not-allowed disabled:opacity-40"
					>
						{t('rebuildNow')}
					</button>
				</section>
			)}

			{/*  Split **into groups** by whether a re-index is needed.
			     It used to put an amber "needs a re-index" chip on each row, and seven of eight rows
			     had one —— colouring seven to distinguish one, which is backwards, and the warning
			     colour that mattered became a background pattern (checked on screen 2026-08-23).
			     One group heading says the same thing **seven times less** and shows better.
			     The criterion is the server's `reindex` flag —— listing them by hand here goes
			     quietly wrong every time a setting is added. */}
			{[true, false].map((needsReindex) => {
				const rows = cfg.settings.filter((s) => s.reindex === needsReindex);
				if (rows.length === 0) return null;
				return (
					<SettingGroup
						key={String(needsReindex)}
						title={needsReindex ? t('requiresRebuild') : t('appliedImmediately')}
						desc={needsReindex
							? t('requiresRebuildDesc')
							: t('appliedImmediatelyDesc')}
					>
						{rows.map((s) => (
							<Row
								key={s.key}
								s={s}
								draft={draft[s.key]}
								onChange={(v) => setDraft((d) => ({ ...d, [s.key]: v }))}
							/>
						))}
					</SettingGroup>
				);
			})}

			{err && (
				<p role="alert" className="rounded-control border border-red-500/40 bg-red-950/30 px-3 py-2 text-sm text-crit">
					{err}
				</p>
			)}

			<div className="flex flex-wrap items-center gap-3">
				<button
					type="button"
					disabled={dirty.length === 0 || saving}
					onClick={() => void save()}
					className="rounded-control border border-ink-700 px-4 py-1.5 text-sm font-medium
					           transition-[color,background-color,box-shadow,scale] duration-150
					           enabled:hover:border-accent enabled:hover:text-accent enabled:active:scale-[0.96]
					           disabled:cursor-not-allowed disabled:opacity-40"
				>
					{saving ? t('saving') : t('save')(dirty.length)}
				</button>
				{dirty.length > 0 && (
					<button
						type="button"
						onClick={() => setDraft({})}
						className="text-xs text-ink-400 underline-offset-2 hover:text-ink-200 hover:underline"
					>
						{t('revert')}
					</button>
				)}
				{saved && <span className="text-xs text-ok">{t('saved')}</span>}
				<span className="text-xs text-ink-500">
					{t('savedToPrefix')}<code className="rounded bg-ink-850 px-1">{cfg.config_path}</code>{t('savedToSuffix')}
				</span>
				{editable.length < cfg.settings.length && (
					<span className="text-xs text-ink-500">
						{t('envLockedCount')(cfg.settings.length - editable.length)}
					</span>
				)}
			</div>
		</div>
	);
}

function Row({ s, draft, onChange }: {
	s: ConfigSetting;
	draft: string | undefined;
	onChange: (v: string) => void;
}) {
	const t = useT(S);
	const locked = s.source === 'env';
	const value = draft ?? String(s.value);
	const changed = draft !== undefined && draft !== String(s.value);

	return (
		<SettingRow
			label={s.label}
			htmlFor={`cfg-${s.key}`}
			desc={s.help}
			badges={<Source source={s.source} envName={s.env} />}
			note={locked ? (
				//  ⚠ The reason it is locked **must** be stated.  A grey input on its own reads as broken.
				<p className="mt-1.5 text-2xs leading-relaxed text-ink-500">
					{t('envSetPrefix')}<code className="rounded bg-ink-850 px-1">{s.env}</code>{t('envSetSuffix')}
				</p>
			) : String(s.value) !== String(s.default) ? (
				<button
					type="button"
					onClick={() => onChange(String(s.default))}
					className="mt-1.5 text-2xs text-ink-400 underline-offset-2 hover:text-ink-200 hover:underline"
				>
					{t('resetDefault')(String(s.default))}
				</button>
			) : undefined}
		>
			{s.min !== null && (
				<span className="text-2xs tabular-nums text-ink-600">{s.min} ~ {s.max}</span>
			)}
			<input
				id={`cfg-${s.key}`}
				type={s.type === 'str' ? 'text' : 'number'}
				//  A number field matches its step to the type —— an integer step on a float makes the
				//  browser mark 1.2 as "invalid".
				step={s.type === 'float' ? 0.05 : s.type === 'int' ? 1 : undefined}
				min={s.min ?? undefined}
				max={s.max ?? undefined}
				value={value}
				disabled={locked}
				onChange={(e) => onChange(e.target.value)}
				//  Numbers are short and strings are long.  Giving both the same width truncated the
				//  embedding model name to `intfloat/multiling…`, so **the screen could not say which model it was**.
				className={`${s.type === 'str' ? 'w-72' : 'w-40'} rounded-control border bg-ink-950 px-2.5 py-1 font-mono text-sm tabular-nums
				            outline-none transition-[color,background-color,box-shadow] duration-150
				            focus:ring-1 focus:ring-accent/60
				            disabled:cursor-not-allowed disabled:opacity-50 ${
					changed ? 'border-amber-500/60 text-amber-100' : 'border-ink-700 text-ink-200'
				}`}
			/>
		</SettingRow>
	);
}

/**
 * **Where the value came from.**  Nothing is attached for a default.
 *
 * There used to be a "default" chip too.  But on a fresh install **all eight** rows are defaults,
 * so eight identical grey chips distinguished nothing at all
 * (checked on screen 2026-08-23).  A marker means something only when attached **to the
 * exception** —— a default is the norm and something touched is the event.
 */
function Source({ source, envName }: { source: string; envName: string }) {
	const t = useT(S);
	if (source === 'default') return null;
	const m = {
		env: { t: `env: ${envName}`, c: 'bg-violet-500/15 text-violet-200' },
		file: { t: t('setHere'), c: 'bg-cyan-500/15 text-cyan-200' },
	}[source] ?? { t: source, c: 'bg-ink-800 text-ink-400' };
	//  `data-source` lets a test look at **the structure, not the text**.  A text check passes even
	//  when the chip renders as `default` instead of the localised word (which really happened).
	return <span data-source={source} className={`rounded px-1.5 py-0.5 text-2xs font-medium ${m.c}`}>{m.t}</span>;
}
