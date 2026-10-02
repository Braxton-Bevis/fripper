# Build the "FRipper" header wordmark: the unchanged FR monogram from
# fundravers-logo.png followed by "ipper", white on transparent, at several
# heights for different display scales (fripper-wordmark-<FR height>.png).
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Drawing
Add-Type -ReferencedAssemblies System.Drawing -TypeDefinition @'
using System;
using System.Drawing;
using System.Drawing.Drawing2D;
using System.Drawing.Imaging;
using System.Drawing.Text;
using System.Runtime.InteropServices;

public static class FRipperWordmark {
    static Rectangle InkBounds(Bitmap bmp) {
        var data = bmp.LockBits(new Rectangle(0, 0, bmp.Width, bmp.Height), ImageLockMode.ReadOnly, PixelFormat.Format32bppArgb);
        var px = new byte[data.Stride * bmp.Height];
        Marshal.Copy(data.Scan0, px, 0, px.Length);
        bmp.UnlockBits(data);
        int minX = bmp.Width, minY = bmp.Height, maxX = -1, maxY = -1;
        for (int y = 0; y < bmp.Height; y++)
            for (int x = 0; x < bmp.Width; x++)
                if (px[y * data.Stride + x * 4 + 2] > 128) {
                    if (x < minX) minX = x; if (x > maxX) maxX = x;
                    if (y < minY) minY = y; if (y > maxY) maxY = y;
                }
        return Rectangle.FromLTRB(minX, minY, maxX + 1, maxY + 1);
    }

    static FontFamily Family() {
        foreach (var name in new[] { "Segoe UI Black", "Arial Black", "Segoe UI" })
            try { return new FontFamily(name); } catch (ArgumentException) { }
        return FontFamily.GenericSansSerif;
    }

    public static void Build(string source, string target, int height) {
        using (var logo = new Bitmap(source))
        using (var family = Family()) {
            var crop = InkBounds(logo);
            float scale = height / (float)crop.Height;
            int markWidth = (int)Math.Round(crop.Width * scale);
            float gap = height * 0.04f;

            // Text outline at a reference size, then fitted to the monogram.
            var path = new GraphicsPath();
            const float em = 100f;
            path.AddString("ipper", family, (int)FontStyle.Regular, em, PointF.Empty, StringFormat.GenericTypographic);
            float baseline = em * family.GetCellAscent(FontStyle.Regular) / family.GetEmHeight(FontStyle.Regular);
            var raw = path.GetBounds();
            float textScale = height * 0.80f / (baseline - raw.Top);   // tallest letter = 80% of the FR height
            var fit = new Matrix();
            fit.Translate(0, -baseline);                                 // baseline to y = 0
            fit.Scale(textScale, textScale, MatrixOrder.Append);
            fit.Shear(-0.17f, 0, MatrixOrder.Append);                    // lean like the R
            path.Transform(fit);
            var b = path.GetBounds();
            var place = new Matrix();
            place.Translate(markWidth + gap - b.Left, height, MatrixOrder.Append); // baseline = FR bottom
            path.Transform(place);
            b = path.GetBounds();

            int width = (int)Math.Ceiling(b.Right + height * 0.04f);
            int fullHeight = (int)Math.Ceiling(Math.Max(height, b.Bottom) + 1);
            using (var canvas = new Bitmap(width, fullHeight, PixelFormat.Format32bppArgb))
            using (var g = Graphics.FromImage(canvas)) {
                g.Clear(Color.Transparent);
                g.SmoothingMode = SmoothingMode.AntiAlias;
                g.InterpolationMode = InterpolationMode.HighQualityBicubic;
                g.PixelOffsetMode = PixelOffsetMode.HighQuality;
                // White ink with the logo's brightness as alpha: no black box behind the mark.
                var matrix = new ColorMatrix(new float[][] {
                    new float[] { 0, 0, 0, 1.4f, 0 }, new float[] { 0, 0, 0, 0, 0 }, new float[] { 0, 0, 0, 0, 0 },
                    new float[] { 0, 0, 0, 0, 0 }, new float[] { 1, 1, 1, -0.3f, 1 } });  // dark grey -> fully clear
                using (var attributes = new ImageAttributes()) {
                    attributes.SetColorMatrix(matrix);
                    g.DrawImage(logo, new Rectangle(0, 0, markWidth, height), crop.X, crop.Y, crop.Width, crop.Height, GraphicsUnit.Pixel, attributes);
                }
                using (var white = new SolidBrush(Color.White)) g.FillPath(white, path);
                canvas.Save(target, ImageFormat.Png);
            }
        }
    }
}
'@
$source = Join-Path $PSScriptRoot 'fundravers-logo.png'
foreach ($height in 32, 40, 48, 64, 192) {   # 192: docs and marketing art
    $target = Join-Path $PSScriptRoot "fripper-wordmark-$height.png"
    [FRipperWordmark]::Build($source, $target, $height)
    Write-Host "Wrote $target"
}
