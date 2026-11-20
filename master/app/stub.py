import html
import time

KINDS = ("personal", "construction", "studio", "cloud", "speedtest", "blog", "maintenance", "custom")

NGINX_404 = ("<html>\r\n<head><title>404 Not Found</title></head>\r\n<body>\r\n"
             "<center><h1>404 Not Found</h1></center>\r\n<hr><center>nginx</center>\r\n</body>\r\n</html>\r\n")

BASE_CSS = """*{box-sizing:border-box}body{margin:0;font:16px/1.65 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;color:#1f2328;background:#fbfbfa}
a{color:#2f6feb;text-decoration:none}a:hover{text-decoration:underline}
.wrap{max-width:720px;margin:0 auto;padding:0 22px}
header{padding:56px 0 28px}h1{font-size:34px;line-height:1.2;margin:0 0 10px;letter-spacing:-.01em}
h2{font-size:20px;margin:36px 0 10px}.lead{font-size:18px;color:#57606a;margin:0}
.muted{color:#6e7781}footer{margin:64px 0 40px;font-size:13px;color:#8c959f}
@media (prefers-color-scheme:dark){body{background:#111315;color:#e6e6e6}.lead,.muted{color:#a0a6ad}footer{color:#7a8088}a{color:#6ea8fe}}"""


def _page(title: str, body: str, extra_css: str = "") -> str:
    t = html.escape(title)
    return (f"<!doctype html><html lang=\"ru\"><head><meta charset=\"utf-8\">"
            f"<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\"><title>{t}</title>"
            f"<style>{BASE_CSS}{extra_css}</style></head><body>{body}</body></html>")


def _personal(title: str, year: str) -> str:
    t = html.escape(title)
    body = f"""<div class="wrap"><header><h1>{t}</h1><p class="lead">Личная страница: заметки, фотографии и небольшие проекты.</p></header>
<h2>Обо мне</h2><p>Инженер, люблю путешествия, фотографию и всё, что можно собрать своими руками. Здесь собираю то, чем хочется поделиться.</p>
<h2>Последние заметки</h2><ul class="list"><li>Как я перевёз домашнюю библиотеку в электронный каталог</li><li>Осенняя поездка: маршрут и впечатления</li><li>Заметки о самодельной метеостанции</li></ul>
<h2>Контакты</h2><p class="muted">Связаться можно через знакомых — публичных контактов здесь нет.</p>
<footer>&copy; {year} {t}</footer></div>"""
    return _page(title, body, ".list{padding-left:20px}.list li{margin:6px 0}")


def _construction(title: str, year: str) -> str:
    t = html.escape(title)
    body = f"""<div class="center"><div class="box"><div class="dot"></div><h1>{t}</h1>
<p class="lead">Сайт находится в разработке. Загляните чуть позже.</p><footer>&copy; {year}</footer></div></div>"""
    css = (".center{min-height:100vh;display:flex;align-items:center;justify-content:center;padding:24px;text-align:center}"
           ".box{max-width:520px}.dot{width:14px;height:14px;border-radius:50%;background:#f0b429;margin:0 auto 22px;"
           "box-shadow:0 0 0 8px rgba(240,180,41,.18)}footer{margin-top:40px}")
    return _page(title, body, css)


def _studio(title: str, year: str) -> str:
    t = html.escape(title)
    body = f"""<div class="wrap"><header><h1>{t}</h1><p class="lead">Небольшая студия: фото, дизайн и вёрстка для частных клиентов.</p></header>
<div class="grid"><div class="card"><b>Фотосъёмка</b><p class="muted">Портреты, семейные и предметные съёмки.</p></div>
<div class="card"><b>Дизайн</b><p class="muted">Логотипы, открытки, оформление соцсетей.</p></div>
<div class="card"><b>Сайты</b><p class="muted">Простые страницы-визитки и лендинги.</p></div></div>
<h2>Как мы работаем</h2><p>Обсуждаем задачу, согласуем сроки и стоимость, показываем черновой вариант и доводим до результата.</p>
<p class="muted">Новые заказы сейчас не принимаем — расписание заполнено.</p>
<footer>&copy; {year} {t}</footer></div>"""
    css = (".grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:14px;margin-top:8px}"
           ".card{border:1px solid rgba(127,127,127,.25);border-radius:12px;padding:16px}.card p{margin:6px 0 0;font-size:14px}")
    return _page(title, body, css)


def _cloud(title: str, year: str) -> str:
    t = html.escape(title)
    body = f"""<div class="center"><form class="box" onsubmit="document.getElementById('e').style.display='block';return false">
<div class="logo">☁</div><h1>{t}</h1><p class="muted">Файловое хранилище. Войдите, чтобы продолжить.</p>
<input type="text" placeholder="Имя пользователя" autocomplete="username" required>
<input type="password" placeholder="Пароль" autocomplete="current-password" required>
<button type="submit">Войти</button><p id="e" class="err">Неверное имя пользователя или пароль.</p>
<footer>&copy; {year} {t}</footer></form></div>"""
    css = (".center{min-height:100vh;display:flex;align-items:center;justify-content:center;padding:24px}"
           ".box{width:100%;max-width:340px;text-align:center}.logo{font-size:44px;line-height:1;margin-bottom:10px;color:#2f6feb}"
           "h1{font-size:26px}input{display:block;width:100%;padding:11px 12px;margin:10px 0;border:1px solid rgba(127,127,127,.35);"
           "border-radius:8px;font:inherit;background:transparent;color:inherit}button{width:100%;padding:11px;border:0;border-radius:8px;"
           "background:#2f6feb;color:#fff;font:inherit;font-weight:600;cursor:pointer}.err{display:none;color:#d1242f;font-size:14px}"
           "footer{margin-top:36px}")
    return _page(title, body, css)


def _speedtest(title: str, year: str) -> str:
    t = html.escape(title)
    body = f"""<div class="center"><div class="box"><h1>{t}</h1><p class="lead">Проверка скорости интернет-соединения</p>
<div class="gauge"><b id="v">0</b><span>Мбит/с</span></div><button id="go" onclick="run()">Начать</button>
<div class="row"><div><span class="muted">Загрузка</span><b id="d">—</b></div><div><span class="muted">Отдача</span><b id="u">—</b></div><div><span class="muted">Пинг</span><b id="p">—</b></div></div>
<footer>&copy; {year} {t}</footer></div></div>
<script>function run(){{var b=document.getElementById('go');b.disabled=true;b.textContent='Измерение…';var n=0,v=document.getElementById('v');
var p=12+Math.round(Math.random()*30),d=40+Math.random()*160,u=10+Math.random()*60;document.getElementById('p').textContent=p+' мс';
var i=setInterval(function(){{n++;v.textContent=(d*Math.min(1,n/30)*(0.9+Math.random()*0.2)).toFixed(1);if(n>=40){{clearInterval(i);
v.textContent=d.toFixed(1);document.getElementById('d').textContent=d.toFixed(1);document.getElementById('u').textContent=u.toFixed(1);
b.disabled=false;b.textContent='Повторить'}}}},100)}}</script>"""
    css = (".center{min-height:100vh;display:flex;align-items:center;justify-content:center;padding:24px;text-align:center}"
           ".box{width:100%;max-width:460px}.gauge{margin:30px auto;width:190px;height:190px;border-radius:50%;border:10px solid rgba(47,111,235,.25);"
           "display:flex;flex-direction:column;align-items:center;justify-content:center}.gauge b{font-size:40px}"
           "button{padding:12px 34px;border:0;border-radius:99px;background:#2f6feb;color:#fff;font:inherit;font-weight:600;cursor:pointer}"
           ".row{display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin-top:28px}.row div{display:flex;flex-direction:column}"
           ".row b{font-size:20px}footer{margin-top:40px}")
    return _page(title, body, css)


def _blog(title: str, year: str) -> str:
    t = html.escape(title)
    posts = [("Как я начал вести дневник наблюдений за погодой", "Полгода записей, простая таблица и пара выводов, которые меня удивили."),
             ("Небольшой поход выходного дня", "Маршрут на 18 километров, что взять с собой и где лучше остановиться на обед."),
             ("Чиню старый плёночный фотоаппарат", "Разобрал, почистил и снова снимаю. Немного о том, с чем пришлось повозиться."),
             ("Книги, прочитанные этой осенью", "Короткие впечатления о пяти книгах и одна, которую не смог дочитать.")]
    items = "".join(f"<article><h2>{html.escape(h)}</h2><p class=\"muted\">{html.escape(p)}</p></article>" for h, p in posts)
    body = f"""<div class="wrap"><header><h1>{t}</h1><p class="lead">Заметки о путешествиях, книгах и домашних проектах.</p></header>
{items}<footer>&copy; {year} {t}</footer></div>"""
    return _page(title, body, "article{padding:6px 0 14px;border-bottom:1px solid rgba(127,127,127,.2)}article h2{margin:22px 0 6px}")


def _maintenance(title: str, year: str) -> str:
    t = html.escape(title)
    body = f"""<div class="center"><div class="box"><div class="ic">⚙</div><h1>{t}</h1>
<p class="lead">На сайте проводятся технические работы. Пожалуйста, зайдите позже.</p><footer>&copy; {year}</footer></div></div>"""
    css = (".center{min-height:100vh;display:flex;align-items:center;justify-content:center;padding:24px;text-align:center}"
           ".box{max-width:520px}.ic{font-size:46px;color:#8c959f;margin-bottom:14px}footer{margin-top:40px}")
    return _page(title, body, css)


def render(kind: str, title: str, custom: str = "") -> str:
    year = time.strftime("%Y")
    title = (title or "").strip() or "Home"
    if kind == "custom" and custom.strip():
        return custom
    if kind == "construction":
        return _construction(title, year)
    if kind == "studio":
        return _studio(title, year)
    if kind == "cloud":
        return _cloud(title, year)
    if kind == "speedtest":
        return _speedtest(title, year)
    if kind == "blog":
        return _blog(title, year)
    if kind == "maintenance":
        return _maintenance(title, year)
    return _personal(title, year)
