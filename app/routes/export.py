from __future__ import annotations

import base64
import datetime
import os
import secrets

from flask import (
    Blueprint, current_app, flash, jsonify, redirect,
    render_template, request, send_file, url_for,
)

bp = Blueprint("export", __name__)


@bp.route('/export/slide', methods=['POST'])
def export_slide():
    data_url = request.form.get('image')
    if not data_url:
        return jsonify({"error": "Missing image payload"}), 400

    if ',' in data_url:
        _, b64data = data_url.split(',', 1)
    else:
        b64data = data_url

    try:
        binary = base64.b64decode(b64data)
    except Exception:
        return jsonify({"error": "Invalid image payload"}), 400

    exports_dir = os.path.join(current_app.static_folder, 'exports')
    os.makedirs(exports_dir, exist_ok=True)

    export_id = secrets.token_hex(8)
    filename = f'{export_id}.png'
    filepath = os.path.join(exports_dir, filename)
    with open(filepath, 'wb') as f:
        f.write(binary)

    image_url = url_for('static', filename=f'exports/{filename}')
    view_url = url_for('.view_export_slide', export_id=export_id)

    return jsonify({"id": export_id, "image_url": image_url, "view_url": view_url})


@bp.route('/export/slide/<string:export_id>')
def view_export_slide(export_id):
    filename = f'{export_id}.png'
    filepath = os.path.join(current_app.static_folder, 'exports', filename)
    if not os.path.exists(filepath):
        flash('Export not found.', 'error')
        return redirect(url_for('pages.compare'))

    ua = (request.user_agent.string or '').lower()
    if 'chrome' in ua and 'firefox' not in ua:
        flash('Note: exported slide downloads tend to work more reliably in Firefox than in Chrome.', 'info')
    image_url = url_for('static', filename=f'exports/{filename}')
    download_url = url_for('.download_export_slide', export_id=export_id)
    return render_template('export_slide.html', image_url=image_url, export_id=export_id, download_url=download_url)


@bp.route('/export/slide/<string:export_id>/delete', methods=['POST'])
def delete_export_slide(export_id):
    filename = f'{export_id}.png'
    filepath = os.path.join(current_app.static_folder, 'exports', filename)
    if os.path.exists(filepath):
        try:
            os.remove(filepath)
            flash('Export deleted.', 'success')
        except OSError:
            flash('Failed to delete export.', 'error')
    else:
        flash('Export not found.', 'error')
    return redirect(url_for('.list_export_slides'))


@bp.route('/export/slide/<string:export_id>/download')
def download_export_slide(export_id):
    filename = f'{export_id}.png'
    filepath = os.path.join(current_app.static_folder, 'exports', filename)
    if not os.path.exists(filepath):
        flash('Export not found.', 'error')
        return redirect(url_for('.list_export_slides'))
    return send_file(
        filepath,
        mimetype='image/png',
        as_attachment=True,
        download_name='benchviz-comparison.png',
    )


@bp.route('/export/slides')
def list_export_slides():
    exports_dir = os.path.join(current_app.static_folder, 'exports')
    exports = []
    if os.path.isdir(exports_dir):
        for name in sorted(os.listdir(exports_dir), reverse=True):
            if not name.lower().endswith('.png'):
                continue
            export_id = os.path.splitext(name)[0]
            filepath = os.path.join(exports_dir, name)
            try:
                mtime = os.path.getmtime(filepath)
                created_at = datetime.datetime.fromtimestamp(mtime)
            except Exception:
                created_at = None
            exports.append({
                'id': export_id,
                'image_url': url_for('static', filename=f'exports/{name}'),
                'view_url': url_for('.view_export_slide', export_id=export_id),
                'created_at': created_at,
            })
    exports.sort(key=lambda x: x['created_at'] or datetime.datetime.min, reverse=True)

    ua = (request.user_agent.string or '').lower()
    if 'chrome' in ua and 'firefox' not in ua:
        flash('Note: exported slide downloads tend to work more reliably in Firefox than in Chrome.', 'info')

    return render_template('export_slides.html', exports=exports)
