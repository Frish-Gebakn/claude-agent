# Claude Agent

**דיין פערזענליכער AI אסיסטענט אויפן קאמפיוטער** — א ווינדאוס פראגראם וואס לאזט דיך שמועסן מיט **Claude Opus 5** (אנטראפיק'ס נייסטע און שטערקסטע AI) אויף היימישן אידיש, און די AI קען טאקע *טון* זאכן אויפן קאמפיוטער.

## 📥 דאונלאוד

**[דאונלאוד די לעצטע ווערסיע »](https://github.com/Frish-Gebakn/claude-agent/releases/latest)** — דאונלאוד `Claude Agent.exe` און לויף עס. איין איינציגע פייל, קיין אינסטאלאציע נישט נייטיג.

---

## וואס עס קען

- 💬 שמועסט אויף אידיש
- 🖥 ראנט cmd / PowerShell קאמאנדס
- 📁 ליינט, שרייבט און טוישט פיילן
- 🌐 קאנטראלירט דיין Chrome בראוזער
- 📧 קאנעקטארס: Gmail, Google Drive, GitHub
- 📱 ענטפערט אויף טעקסטן צו דיין Google Voice נומער
- 🔒 דו קלייבסט וויפיל צוטריט (גאנצע קאמפיוטער אדער נאר איין פאלדער)
- 🔄 אויטא-אפדעיטס

---

## וויאזוי מען שטעלט עס אויף (בקיצור)

1. **דאונלאוד** `Claude Agent.exe` און עפן עס.
2. **שאף אן Anthropic API קי** — גיי צו [console.anthropic.com](https://console.anthropic.com) → עפן א קאנטע → לייג אריין credits (די API קאסט לויט'ן באנוץ) → API Keys → Create Key → קאפיר די קי.
3. אין די פראגראם: **Setup** טאב → פעיסט די קי → **Save settings**.
4. **Computer access** טאב → קלייב וויפיל צוטריט.
5. **Run & Chat** טאב → **Start** → שרייב און שיק!

> די פולע שריט-ביי-שריט אנווייזונגען: זע **[Setup-Instructions.txt](Setup-Instructions.txt)** (אויף אידיש).

---

## ספעציעלע קאמאנדס (אין טשעט אדער דורך SMS)

| קאמאנד | וואס עס טוט |
|--------|-------------|
| `נייע שמועס` / `reset` | הייבט אן א פרישע שמועס |
| `model opus 5` / `fable` / `sonnet` / `haiku` | טוישט מאדעל אויפן פלי |
| `וועלכע מאדעל` / `which model` | זאגט וועלכן מאדעל |
| `help` / `הילף` | ווייזט די ליסטע |

---

## Gmail / Drive / Google Voice — וויאזוי מען שאפט די `credentials.json`

די דאזיגע פיטשערס דארפן א `credentials.json` פייל, וואס מען שאפט **איין מאל (בחינם)** אין [Google Cloud Console](https://console.cloud.google.com). אט זענען די פונקטליכע שריט:

**שריט A — עפן א Google Cloud פראיעקט**
1. גיי צו [console.cloud.google.com](https://console.cloud.google.com) און לאג זיך אריין מיט די Google קאנטע וואס דו ווילסט די AI זאל נוצן.
2. אויבן ביי די פראיעקט-מעניו (לעבן "Google Cloud") → קליק דעם דראפדאון → **New Project**.
3. גיב א נאמען (למשל "Claude Agent") → **Create** → און קלייב יענעם פראיעקט.

**שריט B — אנשטעל די APIs**
1. לינקע מעניו (☰) → **APIs & Services** → **Library**.
2. זוך **Gmail API** → קליק עס → **Enable**.
3. (בלויז פאר Drive) זוך **Google Drive API** → **Enable**.

**שריט C — OAuth consent screen**
1. **APIs & Services** → **OAuth consent screen**.
2. User Type: **External** → **Create**.
3. פיל אויס App name + דיין אימעל (support + developer contact) → **Save and Continue**.
4. Scopes: פשוט **Save and Continue**.
5. **Test users** → **Add users** → לייג צו דיין אייגענעם Google אימעל → **Save and Continue**.

**שריט D — שאף די credentials.json**
1. **APIs & Services** → **Credentials**.
2. **Create Credentials** → **OAuth client ID**.
3. Application type: **Desktop app** → גיב א נאמען → **Create**.
4. אין דעם פאפ-אפ → **Download JSON**. די פייל וואס לאדט אראפ = **דיין `credentials.json`**.

**שריט E — לייג עס אריין אין די פראגראם (און לאג זיך איין)**
1. **Setup** טאב → **Browse…** → קלייב די JSON פייל.
2. קליק **Connect Google Account** → א בראוזער עפנט זיך:
   - קלייב דיין קאנטע.
   - ביי "Google hasn't verified this app": **Advanced** → **Go to Claude Agent (unsafe)** → **Continue**.
   - ערלויב די Gmail (און Drive) דערלויבענישן.
3. פארטיג — דער סטאטוס זאגט "Connected as your@email".

> **⚠️ טו שריט E (Connect Google Account + לאג זיך איין) איידער דו שיקסט דיין ערשטן טעסט-טעקסט.**

**טראבלשוטינג:** "access_denied" → לייג צו דיין אימעל ביי Test users (שריט C-5) • "Gmail API disabled" → ענדיג שריט B • פאלשע קאנטע → מעק אויס `token.json` און פארבינד נאכאמאל.

---

## Google Voice SMS (טעקסטן צו די AI)

כדי צו קענען טעקסטן צו די AI פון דיין טעלעפאן, מוזטו **אנשטעלן אימעל-נאטיפיקאציע ביי Google Voice**:

1. גיי צו [voice.google.com](https://voice.google.com)
2. Settings (⚙) → **Messages**
3. שטעל **אן** די אפציע **"Forward messages to email"**

דעמאלט קומען די טעקסטן אריין אין Gmail, און די פראגראם ענטפערט צוריק פער SMS.

> **⚠️ דער ערשטער מאל:** נאכן אריינלייגן די `credentials.json` פייל, קליק **"Connect Google Account"** (אדער פרוביר עס איין מאל אין די פראגראם) — עס וועט זיך עפענען א בראוזער-פענצטער וואס בעט דיך אריינצולאגן אין דיין אימעל און ערלויבן צוטריט. **טו דאס איין מאל, איידער דו שיקסט דיין ערשטן טעסט-טעקסט.**

*(פאר הילף וויאזוי צו אויפסעטן א Google Voice נומער — זע דעם [אייוועלט אשכול](https://www.ivelt.com/forum/viewtopic.php?t=8647).)*

---

## וויכטיגע נאטיצן

- טייל **קיינמאל נישט** דיינע `config.json` אדער `token.json` — זיי האלטן דיין קי און לאגין.
- "גאנצע קאמפיוטער" צוטריט לאזט די AI טון אלץ וואס *דו* קענסט. נוץ "איין פאלדער" און די ריסטריקטעד-קאמאנדס אויב דו ווילסט מער אפגעהיט זיין.
- די API קאסט געלט לויט'ן באנוץ — האלט אן אויג אויף דיין Billing.

---

לעת עתה ארבעט עס מיט א **Claude (Anthropic)** API קי — מען קען נוצן אלע זייערע מאדעלן. מיט די צייט אי"ה וועט מען אויך צולייגן **Gemini** און **ChatGPT**.

פראגעס, הערות אדער אישוס? לאזט וויסן אינעם אייוועלט אשכול.
