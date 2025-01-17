"""
To push projects asynchronously. Start push: (does not block)

job = push_project_async(mergin_client, '/tmp/my_project')

Then we need to wait until we are finished uploading - either by periodically
calling push_project_is_running(job) that will just return True/False or by calling
push_project_wait(job) that will block the current thread (not good for GUI).
To finish the upload job, we have to call push_project_finalize(job).
"""

import json
import hashlib
import pprint
import tempfile
import concurrent.futures

from .common import UPLOAD_CHUNK_SIZE, ClientError
from .merginproject import MerginProject


class UploadJob:
    """ Keeps all the important data about a pending upload job """

    def __init__(self, project_path, changes, transaction_id, mp, mc, tmp_dir):
        self.project_path = project_path       # full project name ("username/projectname")
        self.changes = changes                 # dictionary of local changes to the project
        self.transaction_id = transaction_id   # ID of the transaction assigned by the server
        self.total_size = 0                    # size of data to upload (in bytes)
        self.transferred_size = 0              # size of data already uploaded (in bytes)
        self.upload_queue_items = []           # list of items to upload in the background
        self.mp = mp                           # MerginProject instance
        self.mc = mc                           # MerginClient instance
        self.tmp_dir = tmp_dir                 # TemporaryDirectory instance for any temp file we need
        self.is_cancelled = False              # whether upload has been cancelled
        self.executor = None                   # ThreadPoolExecutor that manages background upload tasks
        self.futures = []                      # list of futures submitted to the executor
        self.server_resp = None                # server response when transaction is finished

    def dump(self):
        print("--- JOB ---", self.total_size, "bytes")
        for item in self.upload_queue_items:
            print("- {} {} {}".format(item.file_path, item.chunk_index, item.size))
        print("--- END ---")


class UploadQueueItem:
    """ A single chunk of data that needs to be uploaded """

    def __init__(self, file_path, size, transaction_id, chunk_id, chunk_index):
        self.file_path = file_path            # full path to the file
        self.size = size                      # size of the chunk in bytes
        self.chunk_id = chunk_id              # ID of the chunk within transaction
        self.chunk_index = chunk_index        # index (starting from zero) of the chunk within the file
        self.transaction_id = transaction_id  # ID of the transaction

    def upload_blocking(self, mc, mp):

        with open(self.file_path, 'rb') as file_handle:
            file_handle.seek(self.chunk_index * UPLOAD_CHUNK_SIZE)
            data = file_handle.read(UPLOAD_CHUNK_SIZE)

            checksum = hashlib.sha1()
            checksum.update(data)

            mp.log.debug(f"Uploading {self.file_path} part={self.chunk_index}")

            headers = {"Content-Type": "application/octet-stream"}
            resp = mc.post("/v1/project/push/chunk/{}/{}".format(self.transaction_id, self.chunk_id), data, headers)
            resp_dict = json.load(resp)
            mp.log.debug(f"Upload finished: {self.file_path}")
            if not (resp_dict['size'] == len(data) and resp_dict['checksum'] == checksum.hexdigest()):
                try:
                    mc.post("/v1/project/push/cancel/{}".format(self.transaction_id))
                except ClientError:
                    pass
                raise ClientError("Mismatch between uploaded file chunk {} and local one".format(self.chunk_id))


def push_project_async(mc, directory):
    """ Starts push of a project and returns pending upload job """

    mp = MerginProject(directory)
    if mp.has_unfinished_pull():
        raise ClientError("Project is in unfinished pull state. Please resolve unfinished pull and try again.")

    project_path = mp.metadata["name"]
    local_version = mp.metadata["version"]

    mp.log.info("--- version: " + mc.user_agent_info())
    mp.log.info(f"--- start push {project_path}")

    try:
        server_info = mc.project_info(project_path)
    except ClientError as err:
        mp.log.error("Error getting project info: " + str(err))
        mp.log.info("--- push aborted")
        raise
    server_version = server_info["version"] if server_info["version"] else "v0"

    mp.log.info(f"got project info: local version {local_version} / server version {server_version}")

    username = mc.username()
    if username not in server_info["access"]["writersnames"]:
        mp.log.error(f"--- push {project_path} - username {username} does not have write access")
        raise ClientError(f"You do not seem to have write access to the project (username '{username}')")

    if local_version != server_version:
        mp.log.error(f"--- push {project_path} - not up to date (local {local_version} vs server {server_version})")
        raise ClientError("There is a new version of the project on the server. Please update your local copy." +
                          f"\n\nLocal version: {local_version}\nServer version: {server_version}")

    changes = mp.get_push_changes()
    mp.log.debug("push changes:\n" + pprint.pformat(changes))

    tmp_dir = tempfile.TemporaryDirectory(prefix="mergin-py-client-")

    # If there are any versioned files (aka .gpkg) that are not updated through a diff,
    # we need to make a temporary copy somewhere to be sure that we are uploading full content.
    # That's because if there are pending transactions, checkpointing or switching from WAL mode
    # won't work, and we would end up with some changes left in -wal file which do not get
    # uploaded. The temporary copy using geodiff uses sqlite backup API and should copy everything.
    for f in changes["updated"]:
        if mp.is_versioned_file(f["path"]) and "diff" not in f:
            mp.copy_versioned_file_for_upload(f, tmp_dir.name)

    for f in changes["added"]:
        if mp.is_versioned_file(f["path"]):
            mp.copy_versioned_file_for_upload(f, tmp_dir.name)

    # currently proceed storage limit check only if a project is own by a current user.
    if username == project_path.split("/")[0]:
        enough_free_space, freespace = mc.enough_storage_available(changes)
        if not enough_free_space:
            freespace = int(freespace/(1024*1024))
            mp.log.error(f"--- push {project_path} - not enough space")
            raise ClientError("Storage limit has been reached. Only " + str(freespace) + "MB left")

    if not sum(len(v) for v in changes.values()):
        mp.log.info(f"--- push {project_path} - nothing to do")
        return

    # drop internal info from being sent to server
    for item in changes['updated']:
        item.pop('origin_checksum', None)
    data = {
        "version": local_version,
        "changes": changes
    }

    try:
        resp = mc.post(f'/v1/project/push/{project_path}', data, {"Content-Type": "application/json"})
    except ClientError as err:
        mp.log.error("Error starting transaction: " + str(err))
        mp.log.info("--- push aborted")
        raise
    server_resp = json.load(resp)

    upload_files = data['changes']["added"] + data['changes']["updated"]

    transaction_id = server_resp["transaction"] if upload_files else None
    job = UploadJob(project_path, changes, transaction_id, mp, mc, tmp_dir)

    if not upload_files:
        mp.log.info("not uploading any files")
        job.server_resp = server_resp
        push_project_finalize(job)
        return None   # all done - no pending job

    mp.log.info(f"got transaction ID {transaction_id}")

    upload_queue_items = []
    total_size = 0
    # prepare file chunks for upload
    for file in upload_files:
        if 'diff' in file:
            # versioned file - uploading diff
            file_location = mp.fpath_meta(file['diff']['path'])
            file_size = file['diff']['size']
        elif "upload_file" in file:
            # versioned file - uploading full (a temporary copy)
            file_location = file["upload_file"]
            file_size = file["size"]
        else:
            # non-versioned file
            file_location = mp.fpath(file['path'])
            file_size = file['size']

        for chunk_index, chunk_id in enumerate(file["chunks"]):
            size = min(UPLOAD_CHUNK_SIZE, file_size - chunk_index * UPLOAD_CHUNK_SIZE)
            upload_queue_items.append(UploadQueueItem(file_location, size, transaction_id, chunk_id, chunk_index))

        total_size += file_size

    job.total_size = total_size
    job.upload_queue_items = upload_queue_items

    mp.log.info(f"will upload {len(upload_queue_items)} items with total size {total_size}")

    # start uploads in background
    job.executor = concurrent.futures.ThreadPoolExecutor(max_workers=4)
    for item in upload_queue_items:
        future = job.executor.submit(_do_upload, item, job)
        job.futures.append(future)

    return job


def push_project_wait(job):
    """ blocks until all upload tasks are finished """

    concurrent.futures.wait(job.futures)


def push_project_is_running(job):
    """
    Returns true/false depending on whether we have some pending uploads

    It also forwards any exceptions from workers (e.g. some network errors). If an exception
    is raised, it is advised to call push_project_cancel() to abort the job.
    """
    for future in job.futures:
        if future.done() and future.exception() is not None:
            job.mp.log.error("Error while pushing data: " + str(future.exception()))
            job.mp.log.info("--- push aborted")
            raise future.exception()
        if future.running():
            return True
    return False


def push_project_finalize(job):
    """
    To be called when push in the background is finished and we need to do the finalization

    This should not be called from a worker thread (e.g. directly from a handler when push is complete).

    If any of the workers has thrown any exception, it will be re-raised (e.g. some network errors).
    That also means that the whole job has been aborted.
    """

    with_upload_of_files = job.executor is not None

    if with_upload_of_files:
        job.executor.shutdown(wait=True)

        # make sure any exceptions from threads are not lost
        for future in job.futures:
            if future.exception() is not None:
                job.mp.log.error("Error while pushing data: " + str(future.exception()))
                job.mp.log.info("--- push aborted")
                raise future.exception()

    if job.transferred_size != job.total_size:
        error_msg = "Transferred size ({}) and expected total size ({}) do not match!".format(job.transferred_size, job.total_size)
        job.mp.log.error("--- push finish failed! " + error_msg)
        raise ClientError("Upload error: " + error_msg)

    if with_upload_of_files:
        try:
            job.mp.log.info(f"Finishing transaction {job.transaction_id}")
            resp = job.mc.post("/v1/project/push/finish/%s" % job.transaction_id)
            job.server_resp = json.load(resp)
        except ClientError as err:
            # server returns various error messages with filename or something generic
            # it would be better if it returned list of failed files (and reasons) whenever possible
            job.mp.log.error("--- push finish failed! " + str(err))

            # if push finish fails, the transaction is not killed, so we
            # need to cancel it so it does not block further uploads
            job.mp.log.info("canceling the pending transaction...")
            try:
                resp_cancel = job.mc.post("/v1/project/push/cancel/%s" % job.transaction_id)
                job.mp.log.info("cancel response: " + resp_cancel.msg)
            except ClientError as err2:
                job.mp.log.info("cancel response: " + str(err2))
            raise err

    job.mp.metadata = {
        'name': job.project_path,
        'version': job.server_resp['version'],
        'files': job.server_resp["files"]
    }
    try:
        job.mp.apply_push_changes(job.changes)
    except Exception as e:
        job.mp.log.error("Failed to apply push changes: " + str(e))
        job.mp.log.info("--- push aborted")
        raise ClientError("Failed to apply push changes: " + str(e))

    job.tmp_dir.cleanup()   # delete our temporary dir and all its content

    job.mp.log.info("--- push finished - new project version " + job.server_resp['version'])


def push_project_cancel(job):
    """
    To be called (from main thread) to cancel a job that has uploads in progress.
    Returns once all background tasks have exited (may block for a bit of time).
    """

    job.mp.log.info("user cancelled the push...")
    # set job as cancelled
    job.is_cancelled = True

    job.executor.shutdown(wait=True)
    try:
        resp_cancel = job.mc.post("/v1/project/push/cancel/%s" % job.transaction_id)
        job.server_resp = resp_cancel.msg
    except ClientError as err:
        job.mp.log.error("--- push cancelling failed! " + str(err))
        raise err
    job.mp.log.info("--- push cancel response: " + str(job.server_resp))


def _do_upload(item, job):
    """ runs in worker thread """
    if job.is_cancelled:
        return

    item.upload_blocking(job.mc, job.mp)
    job.transferred_size += item.size
