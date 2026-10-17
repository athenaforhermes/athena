module.exports = {
  version: "7.0",
  menu: async (kernel, info) => {
    /**********************************************************************************************
    * 
    * `info` has 4 methods (where `filepath` may be a relative path or an absolute path.):
    * 
    *   - info.local(filepath): get the local variable object of a script running at `filepath`. Example:
    *     
    *     // get local variables for the currently running start.json script
    *     let local = info.local("start.json")
    *     if (local.url) {
    *       // do something with local.url (the 'url' local variable set inside the start.json script)
    *     }
    * 
    *   - info.running(filepath): get the running status of a script at `filepath`. Example:
    * 
    *     // check if install.json script is running
    *     let installing = info.running("install.json")
    *     if (installing) {
    *       ...
    *     }
    * 
    *   - info.exists(filepath): check if a file exists at `filepath`. Example:
    * 
    *     // check if app/venv path exists
    *     let dependency_installed = info.exists("app/venv")
    *     if (dependency_installed) {
    *       ...
    *     }
    * 
    *   - info.path(filepath): get the absolute path of a `fileapth`. Example:
    * 
    *     // get the install.json absolute path
    *     let absolute_path = info.path("install.json")
    * 
    **********************************************************************************************/
    return [{
      default: true,
      icon: "fa-solid fa-power-off",
      text: "Start",
      href: "start.json"
    }]
  }
}
