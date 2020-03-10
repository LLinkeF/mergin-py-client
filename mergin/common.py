
import os


CHUNK_SIZE = 100 * 1024 * 1024

# there is an upper limit for chunk size on server, ideally should be requested from there once implemented
UPLOAD_CHUNK_SIZE = 10 * 1024 * 1024


this_dir = os.path.dirname(os.path.realpath(__file__))


class ClientError(Exception):
    pass


class SyncError(Exception):
    def __init__(self, msg, detail=""):
        super().__init__(msg)
        self.detail = detail


try:
    import dateutil.parser
    from dateutil.tz import tzlocal
except ImportError:
    # this is to import all dependencies shipped with package (e.g. to use in qgis-plugin)
    deps_dir = os.path.join(this_dir, 'deps')
    if os.path.exists(deps_dir):
        import sys
        for f in os.listdir(os.path.join(deps_dir)):
            sys.path.append(os.path.join(deps_dir, f))

        import dateutil.parser
        from dateutil.tz import tzlocal
