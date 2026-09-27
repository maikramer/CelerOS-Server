// Ola Celer — app de exemplo do catalogo CelerOS.
// Padrao dos apps JS: ES5, canvas virtual 240x320, X no canto sup. direito
// sai (o core intercepta; o app nem ve). Tema via System.theme(); strings
// SEM acentos (fonte do dispositivo nao tem glifos acentuados).

var T = System.theme();

// RGB565 aproximando o gradiente da marca: azul #2B4BE0 -> ciano #3EDBF0
var BLUE = 0x2C9C, CYAN = 0x6EBE;

var taps = 0;
var flip = false;

function ctext(s, cx, cy, f, col, bg) {
    System.setTextColor(col, bg);
    var fh = System.fontHeight ? System.fontHeight(f) : (f >= 2 ? 16 : 10);
    System.drawString(s, cx - (System.textWidth(s, f) >> 1), cy - (fh >> 1), f);
}
function hit(t, x, y, w, h) {
    return t.x >= x && t.x <= x + w && t.y >= y && t.y <= y + h;
}
function waitRelease() {
    var guard = 0;
    while (guard < 200) {
        if (!System.getTouch().touched) return;
        System.delay(10);
        guard++;
    }
}
// interpola duas cores RGB565 (t 0..255)
function lerp565(a, b, t) {
    var ar = (a >> 11) & 31, ag = (a >> 5) & 63, ab = a & 31;
    var br = (b >> 11) & 31, bg = (b >> 5) & 63, bb = b & 31;
    return (((ar + (((br - ar) * t) >> 8)) & 31) << 11)
         | (((ag + (((bg - ag) * t) >> 8)) & 63) << 5)
         | ((ab + (((bb - ab) * t) >> 8)) & 31);
}

function draw() {
    System.fillScreen(T.bg);

    // faixa de marca: azul -> ciano
    var i;
    for (i = 0; i < 160; i += 4) {
        System.fillRect(40 + i, 64, 4, 8, lerp565(BLUE, CYAN, (i * 255) / 160));
    }

    ctext("Ola, CelerOS!", 120, 120, 2, T.text, T.bg);
    System.setTextColor(T.textDim, T.bg);
    var sub = "primeiro app da loja";
    System.drawString(sub, 120 - (System.textWidth(sub, 1) >> 1), 142, 1);

    // botao contador (alterna accent/ok a cada toque)
    var bgBtn = flip ? T.ok : T.accent;
    System.fillRoundRect(60, 190, 120, 44, 10, bgBtn);
    ctext("toque! " + taps, 120, 212, 2, T.onAccent, bgBtn);

    System.setTextColor(T.textDim, T.bg);
    System.drawString("celeros.ola v1.0.0", 8, 306, 1);
}

draw();
while (true) {
    var t = System.getTouch();
    if (t.touched) {
        if (hit(t, 60, 190, 120, 44)) {
            waitRelease();
            taps++;
            flip = !flip;
            draw();
        } else {
            waitRelease();
        }
    }
    System.delay(20);
}
