/**
 * `obsidian` ships types only (no main in package.json) —— a module importing it cannot even be
 * resolved in a test.  The stand-in at src/web/obsidian.ts reads matchMedia at top level and dies
 * under node, so only the two things i18n actually uses live here.
 */
export function getLanguage(): string {
	return 'en';
}
export const moment = { locale: (): string => 'en' };
