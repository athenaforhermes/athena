module.exports = {
  daemon: true,
  run: [
    {
      method: "shell.run",
      params: {
        //venv: "../../env",                // Edit this to customize the venv folder path
        venv: "env",
        env: {
          "TRANSFORMERS_VERBOSITY": "info",
          "CUDA_VISIBLE_DEVICES": "0"
        },                   // Edit this to customize environment variables (see documentation)
        //path: "app/src/mochi_preview",                // Edit this to customize the path to start the shell from
        path: "app/demos",
        message: [
          //"python gradio_ui.py --model_dir ../../checkpoint"
          //"python -m mochi_preview.gradio_ui --model_dir ../../checkpoint"
          //"python -m mochi_preview.gradio_ui --model_dir checkpoint"
          "python gradio_ui.py --model_dir ../checkpoint"
        ],
        on: [{
          // The regular expression pattern to monitor.
          // When this pattern occurs in the shell terminal, the shell will return,
          // and the script will go onto the next step.
          "event": "/http:\/\/[0-9.:]+/",   

          // "done": true will move to the next step while keeping the shell alive.
          // "kill": true will move to the next step after killing the shell.
          "done": true
        }]
      }
    },
    {
      // This step sets the local variable 'url'.
      // athena.js reads this local variable to show the "Open WebUI" tab once it is set.
      method: "local.set",
      params: {
        // the input.event is the regular expression match object from the previous step
        url: "{{input.event[0]}}"
      }
    }
  ]
}
