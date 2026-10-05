# item-tracker-ahs1 — إعداد الماسح

المستودع عام؛ الكود وسجلات Actions ظاهرة للعموم. لا تضع رمز تيليجرام أو جلسة أمازون أو ملف التتبع داخل المستودع.

## طريقة التشغيل

- GitHub يشغّل عاملًا تلقائيًا كل 6 ساعات. العامل يشغّل FAST ثم NORMAL، وينتظر 5 دقائق بعد اكتمال الدورة قبل إعادتها.
- ينتهي العامل قبل حد GitHub البالغ 6 ساعات ثم يبدأ العامل التالي تلقائيًا. قد تحصل فجوة قصيرة أو تأخير عند إعادة التشغيل.
- `tracking_amazonyalla.json` و`state.json` يبقيان في Google Drive. يتوقف البرنامج إذا لم يجد ملف التتبع؛ لا ينشئ سجلًا جديدًا بصمت.

## ربط Drive

1. في Google Cloud Console أنشئ مشروعًا، فعّل Google Drive API، وأنشئ Service Account ومفتاح JSON.
2. لا ترفع ملف المفتاح إلى GitHub ولا ترسله في المحادثة. من الملف خذ قيمة `client_email` فقط.
3. شارك مجلد Drive `amazon_session` مع بريد الخدمة بصلاحية Viewer، وشارك مجلد `Amazon_Scanner` بصلاحية Editor.
4. انسخ معرّف كل مجلد من الرابط، وهو الجزء الذي يأتي بعد `/folders/`.
5. في المستودع افتح Settings → Secrets and variables → Actions:
   - Secret `GOOGLE_SERVICE_ACCOUNT_JSON`: محتوى ملف المفتاح JSON كاملًا.
   - Secret `TELEGRAM_BOT_TOKEN`: رمز البوت الجديد.
   - Variable `DRIVE_SESSION_FOLDER_ID`: معرّف مجلد `amazon_session`.
   - Variable `DRIVE_TRACKING_FOLDER_ID`: معرّف مجلد `Amazon_Scanner`.

يلزم وجود `state.json` داخل `amazon_session` و`tracking_amazonyalla.json` داخل `Amazon_Scanner`. لا تغيّر أسماء الملفات. حساب الخدمة يقرأ ملف الجلسة ويحدّث ملف التتبع.

## أول تشغيل

بعد إضافة ملفات المشروع وضبط القيم أعلاه، افتح Actions → Amazon Yalla scanner → Run workflow. تحقق من سجل التشغيل أن عدد المنتجات المحمّل يطابق سجل Drive القديم قبل ترك الجدولة تعمل. لا تضف تشغيلًا عند `pull_request`؛ الـworkflow يعمل بالجدول أو يدويًا فقط.
