module.exports = {
  requires: {
    bundle: "ai"
  },
  run: [
    // Edit this step to customize the git repository to use
    {
      method: "shell.run",
      params: {
        message: [
          "git clone https://github.com/genmoai/models app",
        ]
      }
    },
    // Edit this step with your custom install commands
//    {
//      when: "{{platform === 'win32' && gpu === 'nvidia'}}",
//      method: "shell.run",
//      params: {
//        venv: "env",                // Edit this to customize the venv folder path
//        path: "app",                // Edit this to customize the path to start the shell from
//        message: "pip install https://github.com/oobabooga/flash-attention/releases/download/v2.6.3/flash_attn-2.6.3+cu122torch2.4.1cxx11abiFALSE-cp310-cp310-win_amd64.whl"
//      }
//    },
//    {
//      when: "{{platform === 'linux' && gpu === 'nvidia'}}",
//      method: "shell.run",
//      params: {
//        venv: "env",                // Edit this to customize the venv folder path
//        path: "app",                // Edit this to customize the path to start the shell from
//        message: "pip install https://github.com/Dao-AILab/flash-attention/releases/download/v2.6.3/flash_attn-2.6.3+cu123torch2.4cxx11abiFALSE-cp310-cp310-linux_x86_64.whl"
//      }
//    },
    {
      method: "shell.run",
      params: {
        venv: "demos/env",                // Edit this to customize the venv folder path
        path: "app",                // Edit this to customize the path to start the shell from
        message: [
          "uv pip install wheel",
          "uv pip install -e . --no-build-isolation"
        ]
      }
    },
    // Delete this step if your project does not use torch
    {
      method: "script.start",
      params: {
        uri: "torch.js",
        params: {
          venv: "env",                // Edit this to customize the venv folder path
          path: "app/demos",                // Edit this to customize the path to start the shell from
          // xformers: true   // uncomment this line if your project requires xformers
        }
      }
    },
//    {
//      method: "shell.run",
//      params: {
//        message: "conda install -y -c conda-forge huggingface_hub"
//      }
//    },
    {
      method: "shell.run",
      params: {
        path: "app",                // Edit this to customize the path to start the shell from
        message: "huggingface-cli download genmo/mochi-1-preview --local-dir checkpoint"
      }
    },
    {
      method: "fs.link",
      params: {
        venv: "app/env"
      }
    }
  ]
}
