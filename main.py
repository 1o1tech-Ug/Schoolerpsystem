from flask import request
from app import create_app
import traceback

app = create_app()


@app.before_request
def log_request():
    print(
        f"--> {request.method} {request.path}",
        flush=True
    )


@app.after_request
def log_response(response):
    print(
        f"<-- {request.method} {request.path} "
        f"{response.status_code}",
        flush=True
    )
    return response


@app.errorhandler(500)
def internal_error(e):
    return {
        "error": str(e),
        "trace": traceback.format_exc()
    }, 500


if __name__ == "__main__":
    app.run(
        debug=True,
        port=4000,
        host="0.0.0.0"
    )