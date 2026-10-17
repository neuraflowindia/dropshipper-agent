# Report fonts

Embedded in every client report HTML (`src/product_radar/report_theme.py`), so the report looks the same on every phone and laptop.

| File | Upstream font | Licence |
|---|---|---|
| `nf-serif-text-400.woff2`, `nf-serif-text-600.woff2`, `nf-serif-text-italic-400.woff2`, `nf-serif-head-600.woff2` | Source Serif 4 (Adobe), static Text and Head cuts | SIL Open Font Licence 1.1, `source-serif-4-OFL.txt` |
| `nf-sans-400.woff2`, `nf-sans-500.woff2`, `nf-sans-600.woff2`, `nf-sans-cond-500.woff2`, `nf-sans-cond-600.woff2` | IBM Plex Sans and IBM Plex Sans Condensed (IBM) | SIL Open Font Licence 1.1, `ibm-plex-sans-OFL.txt` |

These are modified versions: static weights, subset to Latin, the rupee sign and a few arrows and marks. Because both upstream fonts declare Reserved Font Names ("Source", "Plex"), the subsets were renamed inside the files to "NF Serif", "NF Serif Head", "NF Sans" and "NF Sans Cond" (16 October 2026); the original copyright and trademark notices are kept in each file. The fonts are used only embedded in NeuraFlow's reports, never sold on their own.
