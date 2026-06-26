# bot/version_endpoint.py
from flask import jsonify

def register_version_endpoint(app):
    """Register /api/version endpoint untuk version check"""
    
    @app.route('/api/version', methods=['GET'])
    def api_version():
        return jsonify({
            "data": {
                "version": "2.0.0"
            },
            "success": True
        })
    
    # Juga tambahkan /version untuk jaga-jaga
    @app.route('/version', methods=['GET'])
    def version():
        return jsonify({
            "data": {
                "version": "2.0.0"
            },
            "success": True
        })