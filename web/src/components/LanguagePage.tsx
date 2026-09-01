import { setLang, useLang, useT, type Lang } from '../i18n/lang';
import S from '../i18n/strings/languagePage';

/**
 * The UI language choice.  English is the default, and choosing Korean shows the pipeline
 * catalogue in the API's source text (Korean).
 * Why two buttons rather than radios —— there are only two options and it applies the instant it is chosen (there is no save button).
 */
export function LanguagePage() {
	const lang = useLang();
	const t = useT(S);
	//  An option's label (the language's name) is always written in that language itself —— it does
	//  not change with the UI language.  Only the hint follows the current UI language, so it is built during the render.
	const options: { id: Lang; label: string; hint: string }[] = [
		{ id: 'en', label: 'English', hint: t('hintEn') },
		{ id: 'ko', label: '한국어', hint: t('hintKo') },
	];
	return (
		<section className="card p-5 flex flex-col gap-4 max-w-xl" aria-labelledby="lang-heading">
			<div>
				<h2 id="lang-heading" className="text-md font-semibold text-ink-100">{t('heading')}</h2>
				<p className="text-xs text-ink-400 mt-1">{t('applies')}</p>
			</div>
			<div role="radiogroup" aria-label={t('heading')} className="flex gap-2">
				{options.map((o) => (
					<button
						key={o.id}
						type="button"
						role="radio"
						aria-checked={lang === o.id}
						onClick={() => setLang(o.id)}
						className={`px-4 py-2 rounded-(--radius-control) text-sm text-left border ${
							lang === o.id ? 'border-accent bg-ink-850 text-ink-100' : 'border-ink-800 text-ink-300 hover:bg-ink-900'
						}`}
					>
						<span className="block font-medium">{o.label}</span>
						<span className="block text-2xs text-ink-400 mono">{o.hint}</span>
					</button>
				))}
			</div>
		</section>
	);
}
