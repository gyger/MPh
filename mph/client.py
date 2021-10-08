﻿"""Provides the wrapper for a Comsol client instance."""

########################################
# Components                           #
########################################
from . import discovery                # back-end discovery
from .model import Model               # model class
from .config import option             # configuration

########################################
# Dependencies                         #
########################################
import jpype                           # Java bridge
import jpype.imports                   # Java object imports
import platform                        # platform information
import os                              # operating system
from pathlib import Path               # file-system paths
from logging import getLogger          # event logging
import faulthandler                    # traceback dumps

########################################
# Globals                              #
########################################
log = getLogger(__package__)           # event log


########################################
# Client                               #
########################################
class Client:
    """
    Manages the Comsol client instance.

    A client can either be a stand-alone instance or it could connect
    to a Comsol server started independently, possibly on a different
    machine on the network.

    Example usage:
    ```python
        import mph
        client = mph.Client(cores=1)
        model = client.load('model.mph')
        model.solve()
        model.save()
        client.remove(model)
    ```

    The number of `cores` (threads) the client instance uses can
    be restricted by specifying a number. Otherwise all available
    cores are used.

    A specific Comsol `version` can be selected if several are
    installed, for example `version='5.3a'`. Otherwise the latest
    version is used.

    Initializes a stand-alone Comsol session if no `port` number is
    specified. Otherwise tries to connect to the Comsol server
    listening at the given port for client connections. The `host`
    address defaults to `'localhost'`, but could be any domain name
    or IP address.

    This class is a wrapper around the [com.comsol.model.util.ModelUtil][1]
    Java class, which itself is wrapped by JPype and can be accessed
    directly via the `.java` attribute. The full Comsol functionality is
    thus available if needed.

    However, as that Comsol class is a singleton, i.e. a static class
    that cannot be instantiated, we can only run one client within the
    same Python process. Separate Python processes would have to be
    created and coordinated in order to work around this limitation.
    Within the same process, `NotImplementedError` is raised if a client
    is already running.

    [1]: https://doc.comsol.com/5.6/doc/com.comsol.help.comsol/api\
/com/comsol/model/util/ModelUtil.html
    """

    ####################################
    # Internal                         #
    ####################################

    def __init__(self, cores=None, version=None, port=None, host='localhost'):

        # Initialize instance attributes.
        self.version    = None
        self.standalone = None
        self.port       = None
        self.host       = None
        self.java       = None

        # Make sure this is the one and only client.
        if jpype.isJVMStarted():
            error = 'Only one client can be instantiated at a time.'
            log.error(error)
            raise NotImplementedError(error)

        # Discover Comsol back-end.
        backend = discovery.backend(version)
        self.version = backend['name']

        # On Windows, turn off fault handlers if enabled.
        # Without this, pyTest will crash when starting the Java VM.
        # See "Errors reported by Python fault handler" in JPype docs.
        # The problem may be the SIGSEGV signal, see JPype issue #886.
        if platform.system() == 'Windows' and faulthandler.is_enabled():
            log.debug('Turning off Python fault handlers.')
            faulthandler.disable()

        # Start the Java virtual machine.
        log.debug(f'JPype version is {jpype.__version__}.')
        log.info('Starting Java virtual machine.')
        java_args = [str(backend['jvm'])]
        if option('classkit'):
            java_args += ['-Dcs.ckl']
        log.debug(f'JVM arguments: {java_args}')
        jpype.startJVM(*java_args,
                       classpath=str(backend['root']/'plugins'/'*'),
                       convertStrings=False)
        log.info('Java virtual machine has started.')

        # Import Comsol client object, a static class, i.e. singleton.
        # See `ModelUtil()` constructor in [1].
        from com.comsol.model.util import ModelUtil as java
        self.java = java

        # Connect to server if port given.
        if port:
            self.standalone = False
            self.connect(port, host)

        # Otherwise initialize a stand-alone client.
        else:
            self.standalone = True
            log.info('Initializing stand-alone client.')

            # Instruct Comsol to limit number of processor cores to use.
            if cores:
                os.environ['COMSOL_NUM_THREADS'] = str(cores)

            # Check correct setup of process environment on Linux/macOS.
            check_environment(backend)

            # Initialize the environment with GUI support disabled.
            # See `initStandalone()` method in [1].
            java.initStandalone(False)

            # Load Comsol settings from disk so as to not just use defaults.
            # This is needed in stand-alone mode, see `loadPreferences()`
            # method in [1].
            java.loadPreferences()

            # Override certain settings not useful in headless operation.
            java.setPreference('updates.update.check', 'off')
            java.setPreference('tempfiles.saving.warnifoverwriteolder', 'off')
            java.setPreference('tempfiles.recovery.autosave', 'off')
            try:
                # Preference not defined on certain systems, see issue #39.
                java.setPreference('tempfiles.recovery.checkforrecoveries',
                                   'off')
            except Exception:
                log.debug('Could not turn off check for recovery files.')
            java.setPreference('tempfiles.saving.optimize', 'filesize')

            # Log that we're done so the start-up time can be inspected.
            log.info('Stand-alone client initialized.')

        # Document instance attributes.
        # The doc-strings are so awkwardly placed at the end here, instead
        # of the first block where we initialize the attributes, because
        # then, for some reason, Sphinx does not put them in source-code
        # order. So this works around it. The assignments are just no-ops.
        self.version = self.version
        """Comsol version (e.g., `'5.3a'`) the client is running on."""
        self.standalone = self.standalone
        """Whether this is a stand-alone client or connected to a server."""
        self.port = self.port
        """Port number on which the client has connected to the server."""
        self.host = self.host
        """Host name or IP address of the server the client is connected to."""
        self.java = self.java
        """Java object that this class is wrapped around."""

    def __repr__(self):
        if self.standalone:
            connection = 'stand-alone'
        elif self.port:
            connection = f'host={self.host}, port={self.port}'
        else:
            connection = 'disconnected'
        return f'{self.__class__.__name__}({connection})'

    def __contains__(self, item):
        if isinstance(item, str):
            if item in self.names():
                return True
        elif isinstance(item, Model):
            if item in self.models():
                return True
        return False

    def __iter__(self):
        yield from self.models()

    def __truediv__(self, name):
        if isinstance(name, str):
            for model in self:
                if name == model.name():
                    break
            else:
                error = f'Model "{name}" has not been loaded by client.'
                log.error(error)
                raise ValueError(error)
            return model
        return NotImplemented

    ####################################
    # Inspection                       #
    ####################################

    @property
    def cores(self):
        """Number of processor cores (threads) the Comsol session is using."""
        cores = self.java.getPreference('cluster.processor.numberofprocessors')
        cores = int(str(cores))
        return cores

    def models(self):
        """Returns all models currently held in memory."""
        return [Model(self.java.model(tag)) for tag in self.java.tags()]

    def names(self):
        """Returns the names of all loaded models."""
        return [model.name() for model in self.models()]

    def files(self):
        """Returns the file-system paths of all loaded models."""
        return [model.file() for model in self.models()]

    ####################################
    # Interaction                      #
    ####################################

    def load(self, file):
        """Loads a model from the given `file` and returns it."""
        file = Path(file).resolve()
        if self.caching() and file in self.files():
            log.info(f'Retrieving "{file.name}" from cache.')
            return self.models()[self.files().index(file)]
        tag = self.java.uniquetag('model')
        log.info(f'Loading model "{file.name}".')
        model = Model(self.java.load(tag, str(file)))
        log.info('Finished loading model.')
        return model

    def caching(self, state=None):
        """
        Enables or disables caching of previously loaded models.

        Caching means that the `load()` method will check if a model
        has been previously loaded from the same file-system path and,
        if so, return the in-memory model object instead of reloading
        it from disk. By default (at start-up) caching is disabled.

        Pass `True` to enable caching, `False` to disable it. If no
        argument is passed, the current state is returned.
        """
        if state is None:
            return option('caching')
        elif state in (True, False):
            option('caching', state)
        else:
            error = 'Caching state can only be set to either True or False.'
            log.error(error)
            raise ValueError(error)

    def create(self, name=None):
        """
        Creates a new model and returns it as a `Model` instance.

        An optional `name` can be supplied. Otherwise the model will
        retain its automatically generated name, like "Model 1".
        """
        java = self.java.createUnique('model')
        model = Model(java)
        if name:
            model.rename(name)
        else:
            name = model.name()
        log.debug(f'Created model "{name}" with tag "{java.tag()}".')
        return model

    def remove(self, model):
        """Removes the given `model` from memory."""
        if isinstance(model, str):
            if model not in self.names():
                error = f'No model named "{model}" exists.'
                log.error(error)
                raise ValueError(error)
            model = self/model
        elif isinstance(model, Model):
            try:
                model.java.tag()
            except Exception:
                error = 'Model does not exist.'
                log.error(error)
                raise ValueError(error)
            if model not in self.models():
                error = 'Model does not exist.'
                log.error(error)
                raise ValueError(error)
        else:
            error = 'Model must either be a model name or Model instance.'
            log.error(error)
            raise TypeError(error)
        name = model.name()
        tag  = model.java.tag()
        log.debug(f'Removing model "{name}" with tag "{tag}".')
        self.java.remove(tag)

    def clear(self):
        """Removes all loaded models from memory."""
        log.debug('Clearing all models from memory.')
        self.java.clear()

    ####################################
    # Remote                           #
    ####################################

    def connect(self, port, host='localhost'):
        """
        Connects the client to a server.

        The Comsol server must be listening at the given `port` for
        client connections. The `host` address defaults to `'localhost'`,
        but could be any domain name or IP address.

        This will fail for stand-alone clients or if the client is already
        connected to a server. In the latter case, `disconnect()` first.
        """
        if self.standalone:
            error = 'Stand-alone clients cannot connect to a server.'
            log.error(error)
            raise RuntimeError(error)
        if self.port:
            error = 'Client already connected to a server. Disconnect first.'
            log.error(error)
            raise RuntimeError(error)
        log.info(f'Connecting to server "{host}" at port {port}.')
        self.java.connect(host, port)
        self.host = host
        self.port = port

    def disconnect(self):
        """
        Disconnects the client from the server.

        Note that the server, unless started with the command-line option
        `-multi on`, will shut down when the client disconnects.
        """
        if self.port:
            log.debug('Disconnecting from server.')
            self.java.disconnect()
            self.host = None
            self.port = None
        else:
            error = 'The client is not connected to a server.'
            log.error(error)
            raise RuntimeError(error)


########################################
# Environment                          #
########################################

def check_environment(backend):
    """Checks the process environment required for a stand-alone client."""
    system = platform.system()
    root = backend['root']
    help = 'Refer to chapter "Limitations" in the documentation for help.'
    if system == 'Windows':
        pass
    elif system == 'Linux':
        var = 'LD_LIBRARY_PATH'
        if var not in os.environ:
            error = f'Library search path {var} not set in environment.'
            log.error(error)
            raise RuntimeError(error + '\n' + help)
        path = os.environ[var].split(os.pathsep)
        lib = root/'lib'/'glnxa64'
        if str(lib) not in path:
            error = f'Folder "{lib}" missing in library search path.'
            log.error(error)
            raise RuntimeError(error + '\n' + help)
        gcc = root/'lib'/'glnxa64'/'gcc'
        if gcc.exists() and str(gcc) not in path:
            log.warning(f'Folder "{gcc}" missing in library search path.')
        gra = str(root/'ext'/'graphicsmagick'/'glnxa64')
        if str(gra) not in path:
            error = f'Folder "{gra}" missing in library search path.'
            log.error(error)
            raise RuntimeError(error + '\n' + help)
        cad = root/'ext'/'cadimport'/'glnxa64'
        if cad.exists() and str(cad) not in path:
            log.warning(f'Folder "{cad}" missing in library search path.')
    elif system == 'Darwin':
        var = 'DYLD_LIBRARY_PATH'
        if var not in os.environ:
            error = f'Library search path {var} not set in environment.'
            log.error(error)
            raise RuntimeError(error + '\n' + help)
        if var in os.environ:
            path = os.environ[var].split(os.pathsep)
        else:
            path = []
        lib = root/'lib'/'maci64'
        if str(lib) not in path:
            error = f'Folder "{lib}" missing in library search path.'
            log.error(error)
            raise RuntimeError(error + '\n' + help)
        gra = root/'ext'/'graphicsmagick'/'maci64'
        if str(gra) not in path:
            error = f'Folder "{gra}" missing in library search path.'
            log.error(error)
            raise RuntimeError(error + '\n' + help)
        cad = root/'ext'/'cadimport'/'maci64'
        if cad.exists() and str(cad) not in path:
            log.warning(f'Folder "{cad}" missing in library search path.')
