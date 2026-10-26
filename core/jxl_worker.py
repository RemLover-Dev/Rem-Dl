"""Isolated, one-file codec worker; never starts the application/server."""
import json
from pathlib import Path


def run(request_path, response_path):
    try:
        from core import shared
        from core import jxl_codec as codec
        from core.jxl_cpu import conversion_threads
        from core.jxl_metadata import read_record, write_record
        request = json.loads(Path(request_path).read_text(encoding='utf-8'))
        threads = request.get('threads', conversion_threads())
        if type(threads) is not int or not 1 <= threads <= conversion_threads():
            raise ValueError('Invalid allocated CPU thread budget')
        codec.JpegXLImagePlugin.DECODE_THREADS = threads
        source = codec.check_file(request['source'])
        work = Path(request_path).parent
        from core.jxl_service import file_hash
        source_hash = file_hash(source)
        mode = request['mode']
        text = shared.read_image_metadata(str(source))
        if mode != 'restore':
            if source.suffix.lower() not in ('.png', '.jpg', '.jpeg', '.webp'):
                raise ValueError('Choose a still PNG, JPG/JPEG or WebP image')
            operation = 'jpeg' if mode == 'auto' and source.suffix.lower() in ('.jpg', '.jpeg') else ('lossless' if mode == 'auto' else mode)
            from core import jxl_highbit
            options = codec.Options(operation, request['quality'], request['effort'], threads=threads)
            options.validate()
            if jxl_highbit.is_highbit(source):
                result = jxl_highbit.convert(source, work, options)
            else:
                result = codec.convert(source, work, options)
            target = Path(result['output'])
            record = {'text': text, 'original_extension': source.suffix.lower(),
                      'original_name': source.name, 'encoding': operation,
                      'width': result['width'], 'height': result['height']}
            record.update(result.pop('highbit', {}))
            write_record(target, record)
            if (read_record(target) or {}).get('text') != text:
                raise ValueError('Tag/category verification failed; output was not published')
            # Check tags through the SAME reader gallery/rescan uses.
            if shared.read_image_metadata(str(target)) != text:
                raise ValueError('Gallery metadata verification failed; output was not published')
            result['output_bytes'] = target.stat().st_size
            result['saving_percent'] = 100 * (1 - result['output_bytes'] / max(result['input_bytes'], 1))
        else:
            if source.suffix.lower() != '.jxl':
                raise ValueError('Choose a JXL image to restore')
            record = read_record(source)
            if record is None:
                raise ValueError('This file has no Rems conversion record. Use JPEG XL Lab to export it as PNG.')
            extension = record.get('original_extension')
            if extension not in ('.jpg', '.jpeg', '.png', '.webp'):
                raise ValueError('Unsupported previous format in conversion record')
            name = Path(record.get('original_name', 'restored' + extension)).name
            # Treat metadata as data, never as an output path or executable.
            if not name.lower().endswith(extension) or name in ('.', '..') or '/' in name or '\\' in name:
                name = 'restored' + extension
            if record.get('bit_depth') == 16:
                if extension != '.png':
                    raise ValueError('16-bit restore requires an original PNG')
                from core import jxl_highbit
                result = jxl_highbit.restore(source, work, record, threads)
                target = Path(result['output'])
                if text is not None:
                    shared.write_image_metadata(str(target), [], [], '', raw_metadata=text)
                if shared.read_image_metadata(str(target)) != text:
                    raise ValueError('Restored 16-bit PNG tag/category verification failed')
            elif record.get('encoding') == 'jpeg':
                result = codec.convert(source, work, codec.Options('restore', threads=threads))
                target = Path(result['output'])
                if shared.read_image_metadata(str(target)) != text:
                    # Tag repair/editing after conversion must survive restore too.
                    # Keep the JPEG image bitstream; update only Rems COM metadata.
                    if text is None:
                        raise ValueError('Restored JPEG tags differ; output was not published')
                    shared.write_image_metadata(str(target), [], [], '', raw_metadata=text)
                    result['metrics'] = None
                    result['note'] = 'JPEG image bitstream restored; Rems tag categories updated to the current embedded values.'
                if shared.read_image_metadata(str(target)) != text:
                    raise ValueError('Restored JPEG tag/category verification failed')
            else:
                image = codec.open_still(source)
                target = work / ('restored' + extension)
                formats = {'.jpg': 'JPEG', '.jpeg': 'JPEG', '.png': 'PNG', '.webp': 'WEBP'}
                kwargs = {'icc_profile': image.info.get('icc_profile')}
                if extension in ('.jpg', '.jpeg'):
                    image = image.convert('RGB')
                    kwargs['quality'] = 95
                elif extension == '.webp':
                    kwargs.update(lossless=True, exact=True)
                image.save(target, format=formats[extension], **kwargs)
                if text is not None:
                    shared.write_image_metadata(str(target), [], [], '', raw_metadata=text)
                if shared.read_image_metadata(str(target)) != text:
                    raise ValueError('Restored tag/category verification failed; output was not published')
                result = {'source': str(source), 'output': str(target), 'input_bytes': source.stat().st_size,
                          'output_bytes': target.stat().st_size, 'metrics': None,
                          'note': 'Previous format recreated. Lossy conversion cannot recover removed detail; original file bytes are only recoverable in reversible JPEG mode.'}
            result['output_bytes'] = target.stat().st_size
            result['output_name'] = name
        if file_hash(source) != source_hash:
            raise ValueError('Source changed during conversion; original retained')
        result['source_sha256'] = source_hash
        result['metadata_verified'] = True
        result['tag_lines'] = len(text.splitlines()) if text else 0
        response = {'ok': True, 'value': result}
    except Exception as exc:
        response = {'ok': False, 'error': str(exc) or type(exc).__name__}
    Path(response_path).write_text(json.dumps(response, ensure_ascii=False), encoding='utf-8')
