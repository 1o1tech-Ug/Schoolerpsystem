from app import create_app
import traceback
app = create_app()


@app.errorhandler(500)
def internal_error(e):
    return {"error": str(e), "trace": traceback.format_exc()}, 500
if __name__ == "__main__":
    app.run(debug=False)#change to false in production
