# ddc_usb

Control brightness, contrast, and other controls of monitors that
expose their DDC/CI over USB.


## Installation

1. Ensure "python3" and "pip3" are available on your system. For Red Hat
   based distributions, you may need to use the following command:
   
   ~~~
   $ sudo yum install python3 python3-pip
   ~~~
   
2. Extract the ddc_usb tarball to a desired directory, for example `/opt`:

   ~~~
   $ sudo tar -C /opt -xf ddc_usb-<version>.tar.gz
   ~~~

3. Open a terminal and run `./setup.sh` inside the extracted directory.

   **Note:** if you extracted to a system directory like `/opt` you may
   need to run setup as root or with a command like 'sudo'.


## Usage

Device access often requires admin priviliges. We recommend running
the `ddc_usb` script either as root or with a tool like "sudo". This
will both ensure you have proper permission and that the program has
access to its required virtual environment.


## Examples

Run the `ddc_usb` script without any arguments for "help" output and
a list of practical examples.
