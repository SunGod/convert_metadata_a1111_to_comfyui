# convert_metadata_a1111_to_comfyui #
Translates A1111 workflow and image metadata within an existing PNG file into ComfyUI-friendly metadata


## Summary ##
This is a handy little Python script that converts the A1111 metadata stored in a PNG image file and converts it all into ComfyUI-friendly metadata format. Many awesome ComfyUI image library management tools don't read A1111 metadata, so this little gem will help bridge that gap, and make those tools treat A1111 images like they were made in ComfyUI. Think of it as a niche A1111-to-ComfyUI metadata Rosetta Stone.


## The long-winded bit ##
Do you have a ton of images that you created using A1111, but none of your ComfyUI image library management tools (like Smart Gallery) can read any of the metadata in them? Sure, those tools might be able to see that the images exist, and they may even let you open up the A1111 metadata in them, but if your experience is like mine, then those tools may not give you all the same capabilities that you'd get with images created natively by ComfyUI. Why? Because the way that A1111 and ComfyUI store their image and workflow metadata are totally different. Yes, you can drop images one-by-one into ComfyUI, and it will do it's best to translate the A1111 metadata into something that ComfyUI can use... but you have to do that one by one... what happens when you have 26,000+ images created by A1111? Trust me, you're not going to drag and drop 26,000+ images into ComfyUI one-by-one just to get usable metadata, that's just crazy.

Enter this little gem of a Python script. 


## How to use it ##
1. Simply save it into the directory with all of your A1111 PNGs. (In my case, I ran it on my ComfyUI output directory, I just moved all my A1111 images there for management simplicity.)

2. Open up a command shell, and then run it on the current directory using:
  "python.exe convert_metadata_a1111_to_comfyui ."
  Don't forget that last period within the quotes, you need to tell it to run on the current directory. Your path to python.exe may vary. For ComfyUI portable users on Windows, you may have python at "ComfyUI_windows_portable/python_embeded\python.exe".
  
3. This script will scan your entire directory, find all of the PNGs, and then one-by-one it will scan each PNG, and if there's any existing A1111 metadata in the image, the script will translate all that A1111 metadata into new ComfyUI-friendly metadata, and then add in into the existing file.

4. If you're using a ComfyUI-based image management library (like Majoor or Smart Gallery), simply refresh/reload the directory you just ran the script against. If you use Smart Gallery, previously for A1111 images you wouldn't get the little "notepad" icon that brings up the metadata organized nice and tidy-like; after this conversion runs (if your A1111 image had valid metadata), you should now see that icon for all successfully converted images. YMMV.


## My results so far ##
It does a pretty good job! I've run this on 26,000 images, and it's captured every A1111 workflow and translated that into a metadata format that ComfyUI can just load up and use. 

Note: If you have old models, VAEs, LORAs or other things from your A1111 workflow that aren't currently in your ComfyUI install, the script will work fine, but when you try to load the converted image into ComfyUI, it'll flag the missing workflow nodes as needing your attention, just like it normally does when something is missing from the workflow.


## FAQ ##
Q: Does this create a new image?
A: No. It merely adds ComfyUI-friendly metadata into your existing image file, using the A1111 metadata from that same image file. None of the existing A1111 metadata is touched, this only adds ComfyUI metadata to it.

Q: Does the image quality change? 
A: No. The image data is left completely untouched.

Q: Can I still use my A1111-created images in A1111 after I run this script?
A: Yes! A1111 will just look for the A1111 metadata in the file (which is left untouched by the script), and will ignore any ComfyUI metadata that was added by the script.

Q: Was this vibe coded?
A: Yes, it was. Look, I just wanted something to get the job done so I could get back to working in ComfyUI. I've used it on 26,000+ images and have had zero problems with image quality, data or performance, so for me it's been awesome. I get that some people may not like this, and I get it, but I have better things to do than learn Python just to build a script that I knocked out with ChatGPT and tested within a 3 hour period. If it helps you, great! If it offends you, I hope you have a nice day! 

Q: Can I take this code and change it, or include it in my other project?
A: Uhhh, I guess? If it's something you're doing for free and will share with the community to make their A1111->ComfyUI transitions easier, go for it, just credit me and this repo. If you're intending to include it in something used for commercial purposes, then no - vibe code your own. lol ;-)

Q: What if my image already has ComfyUI metadata in it? 
A: The script will skip the image and move on. It does not overwrite any existing ComfyUI metadata.

Q: Is it perfect?
A: No, probably not, but I've not seen it break anything or do anything bad. Thus far it's been happily reliable. If you notice anything, tell me and I'll go beat my AI accordingly.
