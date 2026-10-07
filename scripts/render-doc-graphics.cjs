#!/usr/bin/env node
// Rebuild the documentation PNGs and GIF from their editable SVG sources.
// Usage: node scripts/render-doc-graphics.cjs [sharp module path]
const fs = require('node:fs/promises');
const path = require('node:path');
const sharp = require(process.argv[2] || 'sharp');
const images = path.resolve(__dirname, '../docs/images');
const diagrams = ['uu-plus-data-paths-en', 'uu-plus-data-paths-zh-Hans', 'uu-plus-porting-en', 'uu-plus-porting-zh-Hans'];

async function readSvg(name) {
  let svg = await fs.readFile(path.join(images, `${name}.svg`), 'utf8');
  const files = [...svg.matchAll(/<image[^>]+href="([^"]+)"/g)].map(match => match[1]);
  for (const file of new Set(files)) {
    const data = await fs.readFile(path.join(images, file));
    svg = svg.replaceAll(`href="${file}"`, `href="data:image/png;base64,${data.toString('base64')}"`);
  }
  return svg;
}

async function render() {
  for (const name of diagrams) {
    const svg = await readSvg(name);
    await sharp(Buffer.from(svg)).png().toFile(path.join(images, `${name}.png`));
  }
  for (const language of ['en', 'zh-Hans']) {
    const source = await readSvg(`uu-plus-data-paths-animated-${language}`);
    const frames = [];
    let frameInfo;
    const count = 80;
    for (let frame = 0; frame < count; frame += 1) {
      // Match the 160px dash cycle and the return path's -2.4s delay.
      const offset = -160 * frame / count;
      const returnOffset = -160 * ((frame + count / 2) % count) / count;
      const svg = source.replace('</style>',
        `.signal{animation:none;stroke-dashoffset:${offset}}`
        + `.signal.return{stroke-dashoffset:${returnOffset}}</style>`);
      const result = await sharp(Buffer.from(svg)).ensureAlpha().raw().toBuffer({resolveWithObject: true});
      frames.push(result.data);
      frameInfo = result.info;
    }
    await sharp(Buffer.concat(frames), {
      raw: {width: frameInfo.width, height: frameInfo.height * count, channels: 4, pageHeight: frameInfo.height}
    }).gif({loop: 0, delay: Array(count).fill(60), colours: 128, effort: 3, dither: 0}).toFile(path.join(images, `uu-plus-data-flow-${language}.gif`));
  }
  for (const name of ['uu-plus-data-paths', 'uu-plus-porting']) {
    for (const extension of ['svg', 'png']) {
      await fs.copyFile(path.join(images, `${name}-en.${extension}`), path.join(images, `${name}.${extension}`));
    }
  }
  await fs.copyFile(path.join(images, 'uu-plus-data-flow-en.gif'), path.join(images, 'uu-plus-data-flow.gif'));
  await fs.copyFile(path.join(images, 'uu-plus-data-paths-en.png'), path.join(images, 'uu-plus-data-flow.png'));
  console.log('Rendered bilingual PNGs and two looping 80-frame data-flow GIFs (60 ms per frame).');
}
render().catch(error => {console.error(error.message);process.exitCode = 1;});
